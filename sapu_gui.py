#!/usr/bin/env python3
"""
sapu GUI — jendela desktop untuk sapu (Qt 6: PyQt6 atau PySide6).

  sapu gui [FOLDER] [--rescan]

Di Ubuntu/Kubuntu paling mudah memakai Qt sistem agar tema Breeze/KDE ikut terpakai:
  sudo apt install python3-pyqt6
"""
from __future__ import annotations

import argparse
import html
import os
import shutil
import subprocess
import sys
import time
from typing import Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import sapu as core  # noqa: E402
from sapu import RULES, Candidate, MiB, ago, display_label, human, is_stale, tilde  # noqa: E402

try:
    from PyQt6 import QtCore, QtGui, QtWidgets
    Signal = QtCore.pyqtSignal
    BINDING = "PyQt6"
except ImportError:
    try:
        from PySide6 import QtCore, QtGui, QtWidgets
        Signal = QtCore.Signal
        BINDING = "PySide6"
    except ImportError:
        print("GUI butuh PyQt6 atau PySide6.\n"
              "  Ubuntu 24.04+ / Kubuntu:  sudo apt install python3-pyqt6\n"
              "  Lainnya:                  jalankan install.sh (membuat venv berisi PySide6)\n"
              "Sementara itu, coba tampilan terminal:  sapu tui", file=sys.stderr)
        raise SystemExit(2)

Qt = QtCore.Qt
ROLE = Qt.ItemDataRole.UserRole
CHECKED, UNCHECKED = Qt.CheckState.Checked, Qt.CheckState.Unchecked
ALIGN_RIGHT = Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter

SECTIONS = [
    ("aman", "Aman dihapus", "bisa dibuat ulang kapan saja", "#2e9e4f"),
    ("periksa", "Periksa dulu", "baca keterangannya sebelum menghapus", "#c47f00"),
    ("file", "File besar", "file pribadi, putuskan sendiri", "#c47f00"),
    ("info", "Info", "dibersihkan dengan perintahnya sendiri", "#2f7fc1"),
]
SEC_COLOR = {s: c for s, _, _, c in SECTIONS}


def icon(*names: str) -> QtGui.QIcon:
    for n in names:
        ic = QtGui.QIcon.fromTheme(n)
        if not ic.isNull():
            return ic
    return QtGui.QIcon()


def cand_key(c: Candidate) -> str:
    return f"{c.rule}|{c.paths[0]}"


def open_location(path: str) -> None:
    target = path if os.path.isdir(path) else os.path.dirname(path)
    QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(target))


# ---------------------------------------------------------------------------- pekerja (thread)

class ScanWorker(QtCore.QThread):
    progressed = Signal(int, int, str)
    scanned = Signal(object)
    failed = Signal(str)

    def __init__(self, root: str, parent=None) -> None:
        super().__init__(parent)
        self.root = root
        self.cancel = False
        self._last = 0.0

    # dipanggil Scanner dari thread ini
    def tick(self, sc, path: str) -> None:
        if self.cancel:
            raise core.ScanCancelled()
        now = time.monotonic()
        if now - self._last > 0.08:
            self._last = now
            self.progressed.emit(sc.n_dirs, sc.n_files, path)

    def done(self) -> None:
        pass

    def run(self) -> None:
        try:
            self.scanned.emit(core.scan_path(self.root, progress=self))
        except core.ScanCancelled:
            self.failed.emit("cancel")
        except Exception as e:  # noqa: BLE001
            self.failed.emit(str(e))


class CleanWorker(QtCore.QThread):
    cleaned = Signal(object)

    def __init__(self, plan, root: str, parent=None) -> None:
        super().__init__(parent)
        self.plan, self.root = plan, root

    def run(self) -> None:
        in_use = core.in_use_paths()
        outs = []
        for c, m in self.plan:
            outs += core.process_candidate(c, m, self.root, in_use)
        self.cleaned.emit(outs)


class SystemWorker(QtCore.QThread):
    checked = Signal(object)

    def run(self) -> None:
        try:
            self.checked.emit(core.system_items())
        except Exception:  # noqa: BLE001
            self.checked.emit([])


# ---------------------------------------------------------------------------- delegasi bar

class BarDelegate(QtWidgets.QStyledItemDelegate):
    """Menggambar bar persentase di kolom ukuran-relatif."""

    def paint(self, painter, option, index) -> None:
        frac = index.data(ROLE) or 0.0
        super().paint(painter, option, index)
        r = option.rect.adjusted(4, 6, -48, -6)
        if r.width() <= 4:
            return
        painter.save()
        pal = option.palette
        base = pal.color(QtGui.QPalette.ColorRole.Mid)
        base.setAlpha(70)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(base)
        painter.drawRoundedRect(r, 3, 3)
        fill = QtCore.QRect(r)
        fill.setWidth(max(2, int(r.width() * min(1.0, frac))))
        painter.setBrush(pal.color(QtGui.QPalette.ColorRole.Highlight))
        painter.drawRoundedRect(fill, 3, 3)
        painter.setPen(pal.color(QtGui.QPalette.ColorRole.Text))
        txt_rect = option.rect.adjusted(option.rect.width() - 44, 0, -4, 0)
        painter.drawText(txt_rect, ALIGN_RIGHT, f"{frac * 100:.0f}%")
        painter.restore()

    def displayText(self, value, locale) -> str:   # teks digambar sendiri di paint()
        return ""


# ---------------------------------------------------------------------------- jendela utama

class MainWindow(QtWidgets.QMainWindow):
    def __init__(self, root: str, rescan: bool) -> None:
        super().__init__()
        self.root = root
        self.res: Optional[dict] = None
        self.cands: Dict[str, Candidate] = {}
        self.scan_worker: Optional[ScanWorker] = None
        self.clean_worker: Optional[CleanWorker] = None
        self.sys_worker: Optional[SystemWorker] = None
        self.sys_items: Optional[list] = None
        self.history: List[str] = []
        self._updating = False

        self.setWindowTitle("sapu — Pembersih Penyimpanan")
        self.setWindowIcon(icon("user-trash-full", "edit-clear-all", "user-trash"))
        self.resize(1120, 760)
        self.build_ui()

        res = None if rescan else core.load_cache(root)
        if res:
            self.apply_result(res)
            self.status_msg.setText("Memakai hasil pindaian terakhir. Klik Pindai untuk memperbarui.")
        else:
            QtCore.QTimer.singleShot(150, self.start_scan)

    # ------------------------------------------------------------------ tata letak
    def build_ui(self) -> None:
        central = QtWidgets.QWidget()
        lay = QtWidgets.QVBoxLayout(central)
        lay.setContentsMargins(12, 10, 12, 8)
        lay.setSpacing(8)

        # baris folder
        row = QtWidgets.QHBoxLayout()
        self.btn_up = QtWidgets.QToolButton()
        self.btn_up.setIcon(icon("go-up", "go-previous"))
        self.btn_up.setToolTip("Pindai folder induk")
        if self.btn_up.icon().isNull():
            self.btn_up.setText("↑")
        self.btn_up.clicked.connect(self.go_up)
        self.path_edit = QtWidgets.QLineEdit(self.root)
        self.path_edit.returnPressed.connect(lambda: self.start_scan(self.path_edit.text()))
        btn_browse = QtWidgets.QToolButton()
        btn_browse.setIcon(icon("document-open-folder", "folder-open"))
        btn_browse.setToolTip("Pilih folder…")
        if btn_browse.icon().isNull():
            btn_browse.setText("…")
        btn_browse.clicked.connect(self.browse)
        self.btn_scan = QtWidgets.QPushButton(icon("view-refresh"), "Pindai")
        self.btn_scan.clicked.connect(lambda: self.start_scan(self.path_edit.text()))
        row.addWidget(QtWidgets.QLabel("Folder:"))
        row.addWidget(self.btn_up)
        row.addWidget(self.path_edit, 1)
        row.addWidget(btn_browse)
        row.addWidget(self.btn_scan)
        lay.addLayout(row)

        # ringkasan disk
        disk_row = QtWidgets.QHBoxLayout()
        self.disk_bar = QtWidgets.QProgressBar()
        self.disk_bar.setRange(0, 1000)
        self.disk_bar.setTextVisible(True)
        self.disk_bar.setMinimumHeight(22)
        self.scan_info = QtWidgets.QLabel()
        self.scan_info.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        disk_row.addWidget(self.disk_bar, 3)
        disk_row.addWidget(self.scan_info, 2)
        lay.addLayout(disk_row)

        # tab
        self.tabs = QtWidgets.QTabWidget()
        self.tabs.addTab(self.build_rek_tab(), icon("edit-clear-all", "edit-clear"), "Rekomendasi")
        self.tabs.addTab(self.build_folder_tab(), icon("folder"), "Folder terbesar")
        self.tabs.addTab(self.build_system_tab(), icon("computer", "drive-harddisk"), "Sistem")
        self.tabs.currentChanged.connect(self.tab_changed)
        lay.addWidget(self.tabs, 1)
        self.setCentralWidget(central)

        # status bar: progres pindai
        sb = self.statusBar()
        self.status_msg = QtWidgets.QLabel()
        self.busy = QtWidgets.QProgressBar()
        self.busy.setRange(0, 0)
        self.busy.setMaximumWidth(140)
        self.busy.hide()
        self.btn_cancel = QtWidgets.QPushButton(icon("process-stop", "dialog-cancel"), "Batal")
        self.btn_cancel.hide()
        self.btn_cancel.clicked.connect(self.cancel_scan)
        sb.addWidget(self.status_msg, 1)
        sb.addPermanentWidget(self.busy)
        sb.addPermanentWidget(self.btn_cancel)

    def build_rek_tab(self) -> QtWidgets.QWidget:
        w = QtWidgets.QWidget()
        v = QtWidgets.QVBoxLayout(w)
        v.setContentsMargins(6, 8, 6, 6)

        opts = QtWidgets.QHBoxLayout()
        self.chk_stale = QtWidgets.QCheckBox(f"Hanya proyek tak disentuh ≥ {core.STALE_DAYS} hari")
        self.chk_stale.toggled.connect(self.fill_rek)
        self.chk_trash = QtWidgets.QCheckBox("Pindahkan semua ke Tempat Sampah")
        self.chk_trash.setToolTip("Bawaan: yang bisa dibuat ulang dihapus permanen agar ruang langsung lega; "
                                  "file pribadi ke Tempat Sampah.")
        self.chk_trash.toggled.connect(self.selection_changed)
        opts.addWidget(self.chk_stale)
        opts.addWidget(self.chk_trash)
        opts.addStretch(1)
        v.addLayout(opts)

        split = QtWidgets.QSplitter(Qt.Orientation.Vertical)
        self.tree = QtWidgets.QTreeWidget()
        self.tree.setHeaderLabels(["Jenis", "Ukuran", "Aktif terakhir", "Lokasi", "Tindakan"])
        self.tree.setAlternatingRowColors(True)
        self.tree.setUniformRowHeights(True)
        self.tree.setRootIsDecorated(True)
        hdr = self.tree.header()
        hdr.setStretchLastSection(False)
        hdr.setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeMode.Interactive)
        hdr.setSectionResizeMode(3, QtWidgets.QHeaderView.ResizeMode.Stretch)
        self.tree.setColumnWidth(0, 250)
        self.tree.setColumnWidth(1, 95)
        self.tree.setColumnWidth(2, 120)
        self.tree.setColumnWidth(4, 140)
        self.tree.itemChanged.connect(self.item_changed)
        self.tree.currentItemChanged.connect(self.show_detail)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self.rek_menu)
        self.tree.itemDoubleClicked.connect(lambda it, _c: self.open_item(it))
        split.addWidget(self.tree)

        self.detail = QtWidgets.QLabel()
        self.detail.setWordWrap(True)
        self.detail.setTextFormat(Qt.TextFormat.RichText)
        self.detail.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self.detail.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.detail.setMargin(8)
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.detail)
        scroll.setFrameShape(QtWidgets.QFrame.Shape.StyledPanel)
        split.addWidget(scroll)
        split.setStretchFactor(0, 4)
        split.setStretchFactor(1, 1)
        split.setSizes([480, 120])
        v.addWidget(split, 1)

        bottom = QtWidgets.QHBoxLayout()
        self.sel_label = QtWidgets.QLabel()
        f = self.sel_label.font()
        f.setBold(True)
        self.sel_label.setFont(f)
        btn_all = QtWidgets.QPushButton(icon("edit-select-all", "checkbox"), "Centang semua yang aman")
        btn_all.clicked.connect(self.check_all_safe)
        btn_none = QtWidgets.QPushButton("Kosongkan centang")
        btn_none.clicked.connect(lambda: self.set_all_checked(False))
        self.btn_clean = QtWidgets.QPushButton(icon("edit-delete", "user-trash-full"), "Bersihkan…")
        self.btn_clean.setEnabled(False)
        self.btn_clean.clicked.connect(self.clean)
        bottom.addWidget(self.sel_label, 1)
        bottom.addWidget(btn_all)
        bottom.addWidget(btn_none)
        bottom.addWidget(self.btn_clean)
        v.addLayout(bottom)
        return w

    def build_folder_tab(self) -> QtWidgets.QWidget:
        w = QtWidgets.QWidget()
        v = QtWidgets.QVBoxLayout(w)
        v.setContentsMargins(6, 8, 6, 6)
        hint = QtWidgets.QLabel("Klik dua kali sebuah folder untuk memindai isinya lebih dalam. "
                                "Tanda ♻ = punya rekomendasi pembersihan.")
        hint.setEnabled(False)
        v.addWidget(hint)
        self.ftree = QtWidgets.QTreeWidget()
        self.ftree.setHeaderLabels(["Folder", "Ukuran", "Porsi", "Catatan"])
        self.ftree.setAlternatingRowColors(True)
        self.ftree.setColumnWidth(0, 330)
        self.ftree.setColumnWidth(1, 95)
        self.ftree.setColumnWidth(2, 220)
        self.ftree.setItemDelegateForColumn(2, BarDelegate(self.ftree))
        self.ftree.itemDoubleClicked.connect(self.drill)
        self.ftree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.ftree.customContextMenuRequested.connect(self.folder_menu)
        v.addWidget(self.ftree, 1)
        return w

    def build_system_tab(self) -> QtWidgets.QWidget:
        w = QtWidgets.QWidget()
        v = QtWidgets.QVBoxLayout(w)
        v.setContentsMargins(6, 8, 6, 6)
        self.mounts_box = QtWidgets.QVBoxLayout()
        v.addLayout(self.mounts_box)
        self.stree = QtWidgets.QTreeWidget()
        self.stree.setHeaderLabels(["Ukuran", "Item", "Perintah"])
        self.stree.setRootIsDecorated(False)
        self.stree.setAlternatingRowColors(True)
        self.stree.setColumnWidth(0, 95)
        self.stree.setColumnWidth(1, 240)
        self.stree.currentItemChanged.connect(self.show_sys_detail)
        v.addWidget(self.stree, 1)
        self.sys_detail = QtWidgets.QLabel()
        self.sys_detail.setWordWrap(True)
        self.sys_detail.setTextFormat(Qt.TextFormat.RichText)
        self.sys_detail.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.sys_detail.setMinimumHeight(60)
        v.addWidget(self.sys_detail)
        row = QtWidgets.QHBoxLayout()
        note = QtWidgets.QLabel("Perintah ini butuh sudo, jadi tidak dijalankan diam-diam.")
        note.setEnabled(False)
        self.btn_copy = QtWidgets.QPushButton(icon("edit-copy"), "Salin perintah")
        self.btn_copy.clicked.connect(self.copy_cmd)
        self.btn_term = QtWidgets.QPushButton(icon("utilities-terminal", "konsole"), "Jalankan di terminal…")
        self.btn_term.clicked.connect(self.run_in_terminal)
        self.btn_sys_refresh = QtWidgets.QPushButton(icon("view-refresh"), "Periksa ulang")
        self.btn_sys_refresh.clicked.connect(self.start_sys)
        row.addWidget(note, 1)
        row.addWidget(self.btn_sys_refresh)
        row.addWidget(self.btn_copy)
        row.addWidget(self.btn_term)
        v.addLayout(row)
        return w

    # ------------------------------------------------------------------ pindai
    def browse(self) -> None:
        d = QtWidgets.QFileDialog.getExistingDirectory(self, "Pilih folder untuk dipindai", self.root)
        if d:
            self.start_scan(d)

    def go_up(self) -> None:
        if self.history:
            self.start_scan(self.history.pop(), remember=False)
        elif self.root != "/":
            self.start_scan(os.path.dirname(self.root), remember=False)

    def start_scan(self, path: Optional[str] = None, remember: bool = True) -> None:
        if self.scan_worker:
            return
        root = os.path.realpath(os.path.expanduser(path or self.root))
        if not os.path.isdir(root):
            QtWidgets.QMessageBox.warning(self, "sapu", f"Folder tidak ditemukan:\n{root}")
            return
        if remember and self.res and root != self.root:
            self.history.append(self.root)
        self.path_edit.setText(root)
        self.scan_worker = ScanWorker(root, self)
        self.scan_worker.progressed.connect(self.on_progress)
        self.scan_worker.scanned.connect(self.on_scanned)
        self.scan_worker.failed.connect(self.on_scan_failed)
        self.scan_worker.finished.connect(self.on_scan_thread_done)
        self.set_busy(True, f"Memindai {tilde(root)} …")
        self.scan_worker.start()

    def cancel_scan(self) -> None:
        if self.scan_worker:
            self.scan_worker.cancel = True

    def set_busy(self, on: bool, text: str = "") -> None:
        self.busy.setVisible(on)
        self.btn_cancel.setVisible(on and self.scan_worker is not None)
        self.btn_scan.setEnabled(not on)
        self.btn_up.setEnabled(not on)
        self.btn_clean.setEnabled(not on and bool(self.selected()))
        if text:
            self.status_msg.setText(text)

    def on_progress(self, n_dirs: int, n_files: int, path: str) -> None:
        self.status_msg.setText(f"Memindai… {n_dirs:,} folder · {n_files:,} file   {tilde(path)}")

    def on_scanned(self, res: dict) -> None:
        self.apply_result(res)
        self.status_msg.setText(f"Selesai memindai {tilde(self.root)} dalam {res['elapsed']:.1f} detik.")

    def on_scan_failed(self, msg: str) -> None:
        if self.history and self.res and self.path_edit.text() != self.root:
            self.history.pop()
        self.path_edit.setText(self.root)
        self.status_msg.setText("Pemindaian dibatalkan." if msg == "cancel" else f"Gagal memindai: {msg}")

    def on_scan_thread_done(self) -> None:
        self.scan_worker = None
        self.set_busy(False)

    def apply_result(self, res: dict) -> None:
        self.res = res
        self.root = res["root"]
        self.path_edit.setText(self.root)
        self.update_disk()
        self.fill_rek()
        self.fill_folders()

    def update_disk(self) -> None:
        try:
            du = shutil.disk_usage(self.root)
        except OSError:
            return
        pct = du.used / du.total if du.total else 0
        self.disk_bar.setValue(int(pct * 1000))
        self.disk_bar.setFormat(f"Disk terpakai {pct * 100:.0f}%  ·  sisa {human(du.free)} dari {human(du.total)}")
        if pct >= 0.9:
            self.disk_bar.setStyleSheet("QProgressBar::chunk { background-color: #d9433b; }")
        elif pct >= 0.75:
            self.disk_bar.setStyleSheet("QProgressBar::chunk { background-color: #e0a100; }")
        else:
            self.disk_bar.setStyleSheet("")
        if self.res:
            self.scan_info.setText(f"<b>{html.escape(tilde(self.root))}</b>: {human(self.res['total'])} · "
                                   f"{self.res['n_files']:,} file · {self.res['n_dirs']:,} folder")

    # ------------------------------------------------------------------ tab rekomendasi
    def fill_rek(self) -> None:
        if not self.res:
            return
        prev = self.checked_keys()        # pertahankan centang saat daftar diisi ulang
        self._updating = True
        self.tree.clear()
        self.cands = {}
        cands = core.candidates_of(self.res)
        if self.chk_stale.isChecked():
            cands = [c for c in cands if is_stale(c.last_active)]
        bold = QtGui.QFont(self.tree.font())
        bold.setBold(True)
        first_item = None
        for sec, title, sub, color in SECTIONS:
            items = [c for c in cands if core.section_of(c) == sec]
            if not items:
                continue
            head = QtWidgets.QTreeWidgetItem([f"{title}  ({len(items)})", human(sum(c.size for c in items)), "", sub, ""])
            head.setFont(0, bold)
            head.setFont(1, bold)
            head.setForeground(0, QtGui.QBrush(QtGui.QColor(color)))
            head.setTextAlignment(1, ALIGN_RIGHT)
            head.setFirstColumnSpanned(False)
            head.setData(0, ROLE, f"section:{sec}")
            flags = head.flags()
            if sec != "info":
                head.setFlags(flags | Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsAutoTristate)
                head.setCheckState(0, UNCHECKED)
            self.tree.addTopLevelItem(head)
            for c in items:
                key = cand_key(c)
                self.cands[key] = c
                age = ago(c.last_active) + ("  ★" if is_stale(c.last_active) else "")
                loc = f"{len(c.paths)} lokasi" if c.grouped else tilde(c.paths[0])
                action = "—" if sec == "info" else core.MODE_LABEL[self.mode(c)]
                it = QtWidgets.QTreeWidgetItem([display_label(c), human(c.size), age, loc, action])
                it.setData(0, ROLE, key)
                it.setTextAlignment(1, ALIGN_RIGHT)
                it.setToolTip(3, "\n".join(tilde(p) for p in c.paths[:30]))
                if is_stale(c.last_active):
                    it.setForeground(2, QtGui.QBrush(QtGui.QColor("#c47f00")))
                if sec != "info":
                    it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                    it.setCheckState(0, CHECKED if key in prev else UNCHECKED)
                head.addChild(it)
                if c.grouped:
                    for p, s in zip(c.paths, c.sizes):
                        sub_it = QtWidgets.QTreeWidgetItem(["", human(s), "", tilde(p), ""])
                        sub_it.setTextAlignment(1, ALIGN_RIGHT)
                        sub_it.setData(0, ROLE, key)
                        sub_it.setData(3, ROLE, p)
                        sub_it.setFlags(sub_it.flags() & ~Qt.ItemFlag.ItemIsUserCheckable)
                        it.addChild(sub_it)
                if first_item is None:
                    first_item = it
            head.setExpanded(True)
        self._updating = False
        if first_item is None:
            empty = QtWidgets.QTreeWidgetItem(["Tidak ada kandidat pembersihan. Rapi! 🎉"])
            empty.setFlags(Qt.ItemFlag.NoItemFlags)
            self.tree.addTopLevelItem(empty)
            self.detail.setText("")
        else:
            self.tree.setCurrentItem(first_item)
        self.selection_changed()

    def mode(self, c: Candidate) -> str:
        return core.mode_for(c, "trash" if self.chk_trash.isChecked() else None)

    def checked_keys(self) -> set:
        keys = set()
        it = QtWidgets.QTreeWidgetItemIterator(self.tree)
        while it.value():
            item = it.value()
            key = item.data(0, ROLE)
            if (key and key in self.cands and item.flags() & Qt.ItemFlag.ItemIsUserCheckable
                    and item.checkState(0) == CHECKED):
                keys.add(key)
            it += 1
        return keys

    def selected(self) -> List[Candidate]:
        return [self.cands[k] for k in self.checked_keys()]

    def item_changed(self, item, column: int) -> None:
        if not self._updating and column == 0:
            self.selection_changed()

    def selection_changed(self) -> None:
        if self._updating:
            return
        sel = self.selected()
        tot = sum(c.size for c in sel)
        self.sel_label.setText(f"Dicentang: {len(sel)} item · {human(tot)}" if sel
                               else "Centang item yang ingin dibersihkan")
        self.btn_clean.setEnabled(bool(sel) and not self.scan_worker and not self.clean_worker)
        # perbarui kolom Tindakan (bisa berubah karena opsi Tempat Sampah)
        self._updating = True
        it = QtWidgets.QTreeWidgetItemIterator(self.tree)
        while it.value():
            item = it.value()
            key = item.data(0, ROLE)
            if key in self.cands and item.parent() and item.parent().parent() is None:
                c = self.cands[key]
                if core.section_of(c) != "info":
                    item.setText(4, core.MODE_LABEL[self.mode(c)])
            it += 1
        self._updating = False
        self.show_detail(self.tree.currentItem())

    def set_all_checked(self, on: bool, only: Optional[str] = None) -> None:
        self._updating = True
        for i in range(self.tree.topLevelItemCount()):
            head = self.tree.topLevelItem(i)
            sec = (head.data(0, ROLE) or "").replace("section:", "")
            if sec == "info" or not head.flags() & Qt.ItemFlag.ItemIsUserCheckable:
                continue
            if only and sec != only:
                if on:
                    continue
            head.setCheckState(0, CHECKED if on else UNCHECKED)
        self._updating = False
        self.selection_changed()

    def check_all_safe(self) -> None:
        self.set_all_checked(True, only="aman")

    def show_detail(self, item, *_):
        if item is None:
            return
        key = item.data(0, ROLE)
        if not key or key not in self.cands:
            sec = (key or "").replace("section:", "")
            info = {s: (t, d) for s, t, d, _ in SECTIONS}.get(sec)
            self.detail.setText(f"<b>{info[0]}</b> — {info[1]}" if info else "")
            return
        c = self.cands[key]
        r = RULES[c.rule]
        sec = core.section_of(c)
        color = SEC_COLOR[sec]
        parts = [f"<span style='font-size:large'><b style='color:{color}'>{html.escape(display_label(c))}</b>"
                 f" &nbsp; {human(c.size)}</span>"]
        if sec != "info":
            parts.append(f"<br>Tindakan: <b>{core.MODE_LABEL[self.mode(c)]}</b>")
        parts.append(f"<p>{html.escape(r.why)}</p>")
        if r.restore:
            label = "Perintah" if sec == "info" else "Cara membuat ulang"
            parts.append(f"<p>{label}: <code>{html.escape(r.restore)}</code></p>")
        if c.last_active:
            parts.append(f"<p>Proyek terakhir disentuh: {ago(c.last_active)}"
                         + (" — <b>tidak aktif, prioritas dibersihkan</b>" if is_stale(c.last_active) else "")
                         + "</p>")
        paths = "<br>".join(html.escape(tilde(p)) for p in c.paths[:8])
        more = f"<br>… dan {len(c.paths) - 8} lainnya" if len(c.paths) > 8 else ""
        parts.append(f"<p style='color:gray'>{paths}{more}</p>")
        self.detail.setText("".join(parts))

    def open_item(self, item) -> None:
        key = item.data(0, ROLE) if item else None
        if key in self.cands:
            open_location(item.data(3, ROLE) or self.cands[key].paths[0])

    def rek_menu(self, pos) -> None:
        item = self.tree.itemAt(pos)
        key = item.data(0, ROLE) if item else None
        if key not in self.cands:
            return
        c = self.cands[key]
        menu = QtWidgets.QMenu(self)
        menu.addAction(icon("document-open-folder", "folder-open"), "Buka lokasi",
                       lambda: open_location(c.paths[0]))
        menu.addAction(icon("edit-copy"), "Salin jalur",
                       lambda: QtWidgets.QApplication.clipboard().setText("\n".join(c.paths)))
        if os.path.isdir(os.path.dirname(c.paths[0])) and not c.grouped:
            menu.addAction(icon("view-refresh"), "Pindai folder proyeknya",
                           lambda: self.start_scan(os.path.dirname(c.paths[0])))
        menu.exec(self.tree.viewport().mapToGlobal(pos))

    # ------------------------------------------------------------------ bersihkan
    def clean(self) -> None:
        picks = self.selected()
        if not picks:
            return
        plan = [(c, self.mode(c)) for c in picks]
        by_mode: Dict[str, int] = {}
        for c, m in plan:
            by_mode[m] = by_mode.get(m, 0) + c.size
        total = sum(c.size for c in picks)
        rows = "".join(f"<tr><td>{core.MODE_LABEL[m]}</td><td align='right'>&nbsp;&nbsp;<b>{human(s)}</b></td></tr>"
                       for m, s in by_mode.items())
        text = (f"<p>Bersihkan <b>{len(picks)} item</b> dengan total <b>{human(total)}</b>?</p>"
                f"<table>{rows}</table>")
        if "trash" in by_mode:
            text += ("<p style='color:gray'>Item yang dipindah ke Tempat Sampah baru melegakan disk "
                     "setelah Tempat Sampah dikosongkan.</p>")
        if "delete" in by_mode:
            text += "<p>Item <b>hapus permanen</b> tidak bisa dikembalikan, tetapi bisa dibuat ulang.</p>"
        box = QtWidgets.QMessageBox(self)
        box.setWindowTitle("Konfirmasi pembersihan")
        box.setIcon(QtWidgets.QMessageBox.Icon.Warning if "delete" in by_mode else QtWidgets.QMessageBox.Icon.Question)
        box.setTextFormat(Qt.TextFormat.RichText)
        box.setText(text)
        box.setDetailedText("\n".join(f"[{core.MODE_LABEL[m]}] {p}" for c, m in plan for p in c.paths))
        ok = box.addButton("Bersihkan", QtWidgets.QMessageBox.ButtonRole.DestructiveRole)
        box.addButton("Batal", QtWidgets.QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(ok)
        box.exec()
        if box.clickedButton() is not ok:
            return

        self.free_before = shutil.disk_usage(self.root).free
        self.clean_worker = CleanWorker(plan, self.root, self)
        self.clean_worker.cleaned.connect(self.on_cleaned)
        self.clean_worker.finished.connect(lambda: setattr(self, "clean_worker", None))
        self.set_busy(True, f"Membersihkan {len(picks)} item …")
        self.tree.setEnabled(False)
        self.clean_worker.start()

    def on_cleaned(self, outs: list) -> None:
        self.tree.setEnabled(True)
        self.set_busy(False)
        done = {o.path for o in outs if o.status == "ok"}
        freed = sum(o.size for o in outs if o.status == "ok")
        problems = [o for o in outs if o.status in ("skip", "fail")]
        after = shutil.disk_usage(self.root).free
        self.res["total"] = max(0, self.res["total"] - freed)
        for p in done:
            self.res["dir_sizes"].pop(p, None)
        core.forget_paths(self.res, done)
        self._updating = True
        self.tree.clear()
        self._updating = False
        self.cands = {}
        self.fill_rek()
        self.fill_folders()
        self.update_disk()
        self.status_msg.setText(f"Selesai: {human(freed)} dibersihkan.")

        box = QtWidgets.QMessageBox(self)
        box.setWindowTitle("Hasil pembersihan")
        box.setIcon(QtWidgets.QMessageBox.Icon.Information if not problems else QtWidgets.QMessageBox.Icon.Warning)
        box.setTextFormat(Qt.TextFormat.RichText)
        msg = (f"<p><b>{human(freed)}</b> dibersihkan dari {len(done)} lokasi.</p>"
               f"<p>Ruang kosong: {human(self.free_before)} → <b>{human(after)}</b></p>")
        if problems:
            msg += f"<p>{len(problems)} lokasi dilewati atau gagal (lihat detail).</p>"
            box.setDetailedText("\n".join(f"{'GAGAL' if o.status == 'fail' else 'dilewati'}: {o.path} — {o.msg}"
                                          for o in problems))
        box.setText(msg)
        box.exec()

    # ------------------------------------------------------------------ tab folder
    def fill_folders(self) -> None:
        self.ftree.clear()
        if not self.res:
            return
        total = self.res["total"] or 1
        kids: Dict[str, list] = {}
        for p, s in self.res["dir_sizes"].items():
            kids.setdefault(os.path.dirname(p), []).append((s, p))
        tags = {}
        for c in core.candidates_of(self.res):
            for p in c.paths:
                tags[p] = display_label(c) if not c.grouped else RULES[c.rule].label
        folder_icon = icon("folder")

        def make(s, p):
            it = QtWidgets.QTreeWidgetItem([os.path.basename(p) or p, human(s), "", ""])
            it.setIcon(0, folder_icon)
            it.setTextAlignment(1, ALIGN_RIGHT)
            it.setData(0, ROLE, p)
            it.setData(2, ROLE, s / total)
            if p in tags:
                it.setText(3, f"♻ {tags[p]}")
                it.setForeground(3, QtGui.QBrush(QtGui.QColor(SEC_COLOR["aman"])))
            return it

        for s, p in sorted(kids.get(self.root, []), reverse=True):
            it = make(s, p)
            self.ftree.addTopLevelItem(it)
            for s2, p2 in sorted(kids.get(p, []), reverse=True):
                if s2 >= MiB:
                    it.addChild(make(s2, p2))
        if self.ftree.topLevelItemCount():
            for i in range(min(3, self.ftree.topLevelItemCount())):
                self.ftree.topLevelItem(i).setExpanded(True)

    def drill(self, item, _col=0) -> None:
        p = item.data(0, ROLE)
        if p and os.path.isdir(p):
            self.start_scan(p)

    def folder_menu(self, pos) -> None:
        item = self.ftree.itemAt(pos)
        if not item:
            return
        p = item.data(0, ROLE)
        menu = QtWidgets.QMenu(self)
        menu.addAction(icon("view-refresh"), "Pindai folder ini", lambda: self.start_scan(p))
        menu.addAction(icon("document-open-folder", "folder-open"), "Buka di pengelola file",
                       lambda: open_location(p))
        menu.addAction(icon("edit-copy"), "Salin jalur", lambda: QtWidgets.QApplication.clipboard().setText(p))
        menu.exec(self.ftree.viewport().mapToGlobal(pos))

    # ------------------------------------------------------------------ tab sistem
    def tab_changed(self, idx: int) -> None:
        if idx == 2 and self.sys_items is None and not self.sys_worker:
            self.start_sys()

    def start_sys(self) -> None:
        if self.sys_worker:
            return
        while self.mounts_box.count():
            w = self.mounts_box.takeAt(0).widget()
            if w:
                w.deleteLater()
        for m in core.real_mounts():
            try:
                du = shutil.disk_usage(m)
            except OSError:
                continue
            bar = QtWidgets.QProgressBar()
            bar.setRange(0, 1000)
            pct = du.used / du.total if du.total else 0
            bar.setValue(int(pct * 1000))
            bar.setFormat(f"{m}   ·   {pct * 100:.0f}% terpakai   ·   sisa {human(du.free)} dari {human(du.total)}")
            if pct >= 0.9:
                bar.setStyleSheet("QProgressBar::chunk { background-color: #d9433b; }")
            self.mounts_box.addWidget(bar)
        self.stree.clear()
        loading = QtWidgets.QTreeWidgetItem(["", "Memeriksa apt, journal, snap, flatpak, docker …"])
        self.stree.addTopLevelItem(loading)
        self.btn_sys_refresh.setEnabled(False)
        self.sys_worker = SystemWorker(self)
        self.sys_worker.checked.connect(self.on_sys)
        self.sys_worker.finished.connect(lambda: setattr(self, "sys_worker", None))
        self.sys_worker.start()

    def on_sys(self, items: list) -> None:
        self.btn_sys_refresh.setEnabled(True)
        self.sys_items = items
        self.stree.clear()
        if not items:
            self.stree.addTopLevelItem(QtWidgets.QTreeWidgetItem(["", "Tidak ada sampah sistem yang berarti. Rapi!"]))
            self.sys_detail.setText("")
            return
        for i, (size, title, cmd, _note) in enumerate(items):
            it = QtWidgets.QTreeWidgetItem([human(size) if size is not None else "—", title, cmd])
            it.setTextAlignment(0, ALIGN_RIGHT)
            it.setData(0, ROLE, i)
            self.stree.addTopLevelItem(it)
        self.stree.setCurrentItem(self.stree.topLevelItem(0))

    def current_sys(self):
        it = self.stree.currentItem()
        if not it or it.data(0, ROLE) is None or not self.sys_items:
            return None
        return self.sys_items[it.data(0, ROLE)]

    def show_sys_detail(self, *_):
        cur = self.current_sys()
        self.btn_copy.setEnabled(bool(cur))
        self.btn_term.setEnabled(bool(cur) and not cur[2].startswith("sapu "))
        if not cur:
            self.sys_detail.setText("")
            return
        size, title, cmd, note = cur
        self.sys_detail.setText(f"<b>{html.escape(title)}</b><br>{html.escape(note)}"
                                f"<br><code>$ {html.escape(cmd)}</code>")

    def copy_cmd(self) -> None:
        cur = self.current_sys()
        if cur:
            QtWidgets.QApplication.clipboard().setText(cur[2])
            self.status_msg.setText("Perintah disalin ke clipboard.")

    def run_in_terminal(self) -> None:
        cur = self.current_sys()
        if not cur:
            return
        cmd = cur[2].split("   (")[0].strip()    # buang catatan dalam kurung
        term = next((t for t in ("konsole", "x-terminal-emulator", "gnome-terminal", "xterm") if shutil.which(t)), None)
        if not term:
            QtWidgets.QMessageBox.information(self, "sapu", "Terminal tidak ditemukan. Salin perintahnya saja.")
            return
        ans = QtWidgets.QMessageBox.question(
            self, "Jalankan di terminal",
            f"Buka {term} dan jalankan perintah berikut?\n\n{cmd}\n\nAnda akan diminta kata sandi sudo di terminal.")
        if ans != QtWidgets.QMessageBox.StandardButton.Yes:
            return
        script = f"{cmd}; echo; read -rp 'Selesai. Tekan Enter untuk menutup…' _"
        args = {"konsole": ["konsole", "-e", "bash", "-c", script],
                "gnome-terminal": ["gnome-terminal", "--", "bash", "-c", script]}.get(
            term, [term, "-e", "bash", "-c", script])
        subprocess.Popen(args, start_new_session=True)

    def closeEvent(self, event) -> None:
        if self.scan_worker:
            self.scan_worker.cancel = True
            self.scan_worker.wait(3000)
        if self.clean_worker:
            self.clean_worker.wait()
        super().closeEvent(event)


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="sapu gui", description="Jendela desktop sapu.")
    p.add_argument("path", nargs="?", default="~", help="folder yang dipindai (default: home)")
    p.add_argument("--rescan", action="store_true", help="abaikan hasil pindaian sebelumnya")
    p.add_argument("--screenshot", metavar="PNG", help=argparse.SUPPRESS)   # untuk pengujian
    p.add_argument("--tab", type=int, default=0, help=argparse.SUPPRESS)
    args = p.parse_args(argv)
    root = os.path.realpath(os.path.expanduser(args.path))
    if not os.path.isdir(root):
        print(f"Folder tidak ditemukan: {args.path}", file=sys.stderr)
        return 2

    app = QtWidgets.QApplication(sys.argv[:1])
    app.setApplicationName("sapu")
    app.setApplicationDisplayName("sapu")
    app.setDesktopFileName("sapu")
    win = MainWindow(root, args.rescan)
    win.show()
    if args.screenshot:
        def shot():
            if win.scan_worker or win.sys_worker:
                QtCore.QTimer.singleShot(200, shot)
                return
            if args.tab and win.tabs.currentIndex() != args.tab:
                win.tabs.setCurrentIndex(args.tab)
                QtCore.QTimer.singleShot(600, shot)
                return
            win.grab().save(args.screenshot)
            app.quit()
        QtCore.QTimer.singleShot(800, shot)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
