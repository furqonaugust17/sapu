#!/usr/bin/env python3
"""
sapu TUI — tampilan terminal interaktif untuk sapu (curses, tanpa dependensi).

  sapu tui [FOLDER] [--rescan]

Tombol: ↑↓ pindah · Spasi centang · a semua yang aman · c bersihkan · Tab ganti tab · ? bantuan · q keluar
"""
from __future__ import annotations

import argparse
import curses
import locale
import os
import shutil
import subprocess
import sys
import textwrap
import threading
import time
from typing import List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import sapu as core  # noqa: E402
from sapu import RULES, Candidate, MiB, ago, display_label, human, is_stale, tilde  # noqa: E402

VIEWS = [("rek", "Rekomendasi"), ("folder", "Folder terbesar"), ("sistem", "Sistem")]
SECTIONS = [
    ("aman", "AMAN DIHAPUS", "bisa dibuat ulang kapan saja"),
    ("periksa", "PERIKSA DULU", "baca keterangannya sebelum menghapus"),
    ("file", "FILE BESAR", "file pribadi, putuskan sendiri"),
    ("info", "INFO", "dibersihkan dengan perintahnya sendiri"),
]
HELP = [
    "Navigasi",
    "  ↑ ↓  j k        pindah baris",
    "  PgUp PgDn       pindah satu halaman",
    "  Tab  1 2 3      ganti tab",
    "",
    "Rekomendasi",
    "  Spasi           centang / batal centang",
    "  a               centang semua yang AMAN",
    "  n               hapus semua centang",
    "  f               hanya proyek tak disentuh ≥ 90 hari",
    "  t               paksa semua ke Tempat Sampah",
    "  c  Delete       bersihkan yang dicentang",
    "",
    "Folder terbesar",
    "  Enter  →        pindai folder itu",
    "  Backspace  ←    kembali ke folder sebelumnya",
    "",
    "Umum",
    "  o               buka lokasi di pengelola file",
    "  y               salin perintah (tab Sistem)",
    "  r               pindai ulang",
    "  q               keluar",
]


class ProgressProxy:
    """Menerima progres dari Scanner (thread lain) dan bisa membatalkannya."""

    def __init__(self) -> None:
        self.n_dirs = 0
        self.n_files = 0
        self.path = ""
        self.cancel = False

    def tick(self, sc, path: str) -> None:
        self.n_dirs, self.n_files, self.path = sc.n_dirs, sc.n_files, path
        if self.cancel:
            raise core.ScanCancelled()

    def done(self) -> None:
        pass


def open_in_file_manager(path: str) -> bool:
    target = path if os.path.isdir(path) else os.path.dirname(path)
    for cmd in (["xdg-open", target], ["dolphin", target]):
        if shutil.which(cmd[0]):
            try:
                subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 start_new_session=True)
                return True
            except OSError:
                pass
    return False


def copy_to_clipboard(text: str) -> bool:
    cands = [
        ["qdbus6", "org.kde.klipper", "/klipper", "setClipboardContents", text],   # KDE Plasma 6
        ["qdbus", "org.kde.klipper", "/klipper", "setClipboardContents", text],    # KDE Plasma 5
        ["wl-copy", text],
    ]
    for cmd in cands:
        if shutil.which(cmd[0]):
            r = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if r.returncode == 0:
                return True
    for cmd in (["xclip", "-selection", "clipboard"], ["xsel", "-b", "-i"]):
        if shutil.which(cmd[0]):
            r = subprocess.run(cmd, input=text.encode(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if r.returncode == 0:
                return True
    return False


def cand_key(c: Candidate) -> str:
    return f"{c.rule}|{c.paths[0]}"


class App:
    def __init__(self, scr, root: str, rescan: bool) -> None:
        self.scr = scr
        self.root = root
        self.res: Optional[dict] = None
        self.cands: List[Candidate] = []
        self.checked: set = set()
        self.view = "rek"
        self.stale_only = False
        self.force: Optional[str] = None          # None | "trash"
        self.msg = ""
        self.msg_time = 0.0
        self.history: List[str] = []
        # status pemindaian
        self.scan_thread: Optional[threading.Thread] = None
        self.prog: Optional[ProgressProxy] = None
        self.scan_out: Optional[dict] = None
        self.scan_err = ""
        self.scan_root = root
        # kursor per tab: (indeks kursor, baris teratas)
        self.cur = {"rek": 0, "folder": 0, "sistem": 0}
        self.top = {"rek": 0, "folder": 0, "sistem": 0}
        # tab sistem
        self.sys_items: Optional[list] = None
        self.sys_thread: Optional[threading.Thread] = None
        self.sys_out: Optional[list] = None

        if not rescan:
            self.res = core.load_cache(root)
        if self.res:
            self.rebuild()
            self.flash("Memakai hasil pindaian terakhir · tekan r untuk memindai ulang")
        else:
            self.start_scan(root)

    # ------------------------------------------------------------------ data
    def flash(self, text: str) -> None:
        self.msg, self.msg_time = text, time.monotonic()

    @property
    def scanning(self) -> bool:
        return self.scan_thread is not None

    def start_scan(self, root: str) -> None:
        self.prog = ProgressProxy()
        self.scan_root = root
        self.scan_out, self.scan_err = None, ""
        prog = self.prog

        def work():
            try:
                self.scan_out = core.scan_path(root, progress=prog)
            except core.ScanCancelled:
                self.scan_err = "cancel"
            except Exception as e:  # noqa: BLE001
                self.scan_err = str(e)

        self.scan_thread = threading.Thread(target=work, daemon=True)
        self.scan_thread.start()

    def poll_scan(self) -> None:
        if not self.scan_thread or self.scan_thread.is_alive():
            return
        self.scan_thread = None
        if self.scan_out:
            self.res = self.scan_out
            self.root = self.res["root"]
            self.checked.clear()
            self.cur.update(rek=0, folder=0)
            self.top.update(rek=0, folder=0)
            self.rebuild()
            self.flash(f"Selesai memindai {tilde(self.root)} dalam {self.res['elapsed']:.1f} dtk")
        else:
            if self.history and self.scan_root != self.root:
                self.history.pop()
            self.flash("Pemindaian dibatalkan" if self.scan_err == "cancel" else f"Gagal: {self.scan_err}")

    def poll_sys(self) -> None:
        if self.sys_thread and not self.sys_thread.is_alive():
            self.sys_thread = None
            self.sys_items = self.sys_out or []

    def start_sys(self) -> None:
        def work():
            try:
                self.sys_out = core.system_items()
            except Exception:  # noqa: BLE001
                self.sys_out = []
        self.sys_thread = threading.Thread(target=work, daemon=True)
        self.sys_thread.start()

    def rebuild(self) -> None:
        cands = core.candidates_of(self.res) if self.res else []
        if self.stale_only:
            cands = [c for c in cands if is_stale(c.last_active)]
        self.cands = cands
        keys = {cand_key(c) for c in cands}
        self.checked &= keys
        self.cur["rek"] = min(self.cur["rek"], max(0, len(self.rek_items()) - 1))

    def rek_items(self) -> List[Candidate]:
        out = []
        for sec, _, _ in SECTIONS:
            out += [c for c in self.cands if core.section_of(c) == sec]
        return out

    def rek_rows(self) -> List[Tuple[str, object]]:
        rows: List[Tuple[str, object]] = []
        for sec, title, sub in SECTIONS:
            items = [c for c in self.cands if core.section_of(c) == sec]
            if not items:
                continue
            if rows:
                rows.append(("blank", None))
            rows.append(("head", (sec, title, sub, sum(c.size for c in items))))
            rows += [("item", c) for c in items]
        return rows

    def folder_rows(self) -> List[Tuple[int, int, str]]:
        if not self.res:
            return []
        kids: dict = {}
        for p, s in self.res["dir_sizes"].items():
            kids.setdefault(os.path.dirname(p), []).append((s, p))
        rows = []
        for s, p in sorted(kids.get(self.root, []), reverse=True):
            rows.append((0, s, p))
            for s2, p2 in sorted(kids.get(p, []), reverse=True)[:8]:
                if s2 >= MiB:
                    rows.append((1, s2, p2))
        return rows

    def selected(self) -> List[Candidate]:
        return [c for c in self.cands if cand_key(c) in self.checked and core.section_of(c) != "info"]

    # ------------------------------------------------------------------ gambar
    def put(self, y: int, x: int, text: str, attr: int = 0, win=None) -> None:
        win = win or self.scr
        h, w = win.getmaxyx()
        if y < 0 or y >= h or x >= w:
            return
        text = text[: max(0, w - x)]
        try:
            win.addstr(y, x, text, attr)
        except curses.error:
            pass   # menulis di pojok kanan bawah selalu "gagal" di curses

    def color(self, name: str) -> int:
        idx = {"aman": 1, "periksa": 2, "file": 2, "info": 3, "accent": 3, "danger": 4, "title": 5}[name]
        return curses.color_pair(idx) if curses.has_colors() else 0

    def draw(self) -> None:
        s = self.scr
        s.erase()
        h, w = s.getmaxyx()
        if h < 14 or w < 64:
            self.put(0, 0, "Perbesar jendela terminal (min 64×14).", curses.A_BOLD)
            s.refresh()
            return

        # baris judul + tab
        self.put(0, 0, " " * w, self.color("title"))
        self.put(0, 1, " sapu ", self.color("title") | curses.A_BOLD)
        x = 9
        for i, (vid, name) in enumerate(VIEWS, 1):
            label = f" {i} {name} "
            attr = curses.A_REVERSE | curses.A_BOLD if vid == self.view else self.color("title")
            self.put(0, x, label, attr)
            x += len(label) + 1
        self.put(0, w - 10, " ? bantuan", self.color("title"))

        # bar disk
        try:
            du = shutil.disk_usage(self.root)
            pct = du.used / du.total if du.total else 0
            col = self.color("danger") if pct >= .9 else self.color("periksa") if pct >= .75 else self.color("aman")
            self.put(1, 1, "Disk ", curses.A_DIM)
            self.put(1, 6, core.bar(pct, 20), col)
            info = f" {pct * 100:3.0f}%  sisa {human(du.free)} dari {human(du.total)}"
            if self.res:
                info += f"   ·   {tilde(self.root)}: {human(self.res['total'])}, {self.res['n_files']:,} file"
            self.put(1, 26, info)
        except OSError:
            pass

        body_top, detail_h = 3, 5
        body_h = h - body_top - detail_h - 1

        if self.scanning:
            self.draw_scanning(body_top, body_h, w)
        elif self.view == "rek":
            self.draw_rek(body_top, body_h, w)
        elif self.view == "folder":
            self.draw_folder(body_top, body_h, w)
        else:
            self.draw_sistem(body_top, body_h, w)

        # garis bawah + bantuan tombol
        self.put(h - detail_h - 1, 0, "─" * w, curses.A_DIM)
        if not self.scanning:
            self.draw_detail(h - detail_h, detail_h - 1, w)
        self.draw_status(h - 1, w)
        s.refresh()

    def draw_scanning(self, y: int, hh: int, w: int) -> None:
        p = self.prog
        spin = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"[int(time.monotonic() * 10) % 10]
        mid = y + hh // 2 - 2
        self.put(mid, 4, f"{spin}  Memindai {tilde(self.scan_root)} …", curses.A_BOLD)
        if p:
            self.put(mid + 2, 7, f"{p.n_dirs:,} folder · {p.n_files:,} file", self.color("accent"))
            self.put(mid + 3, 7, core.shorten(tilde(p.path), w - 10), curses.A_DIM)
        self.put(mid + 5, 7, "Esc / q untuk membatalkan", curses.A_DIM)

    def scroll(self, view: str, cur_row: int, hh: int) -> int:
        top = self.top[view]
        if cur_row < top:
            top = cur_row
        elif cur_row >= top + hh:
            top = cur_row - hh + 1
        self.top[view] = max(0, top)
        return self.top[view]

    def draw_rek(self, y: int, hh: int, w: int) -> None:
        rows = self.rek_rows()
        if not rows:
            msg = ("Tidak ada proyek yang tak disentuh ≥ 90 hari. Tekan f untuk menampilkan semua."
                   if self.stale_only else "Tidak ada kandidat pembersihan. Rapi!")
            self.put(y + 1, 3, msg, self.color("aman") | curses.A_BOLD)
            return
        items = self.rek_items()
        cur = min(self.cur["rek"], len(items) - 1)
        self.cur["rek"] = cur
        cur_c = items[cur]
        cur_row = next(i for i, r in enumerate(rows) if r[0] == "item" and r[1] is cur_c)
        # sertakan judul seksi bila kursor di item pertama
        top = self.scroll("rek", cur_row, hh)
        if cur_row - 1 >= 0 and rows[cur_row - 1][0] == "head" and top == cur_row:
            top = self.top["rek"] = max(0, cur_row - 1)
        loc_w = max(10, w - 4 - 10 - 2 - 20 - 15 - 2)
        for i, (kind, data) in enumerate(rows[top: top + hh]):
            yy = y + i
            if kind == "head":
                sec, title, sub, tot = data
                self.put(yy, 1, title, self.color(sec) | curses.A_BOLD)
                self.put(yy, 2 + len(title), f"— {sub}", curses.A_DIM)
                tail = f"total {human(tot)}"
                self.put(yy, w - len(tail) - 2, tail, curses.A_BOLD)
            elif kind == "item":
                c: Candidate = data
                sec = core.section_of(c)
                on = cand_key(c) in self.checked
                box = " i " if sec == "info" else ("[✓]" if on else "[ ]")
                age = ago(c.last_active) + (" ★" if is_stale(c.last_active) else "")
                loc = f"{len(c.paths)} lokasi · {tilde(c.paths[0])}" if c.grouped else tilde(c.paths[0])
                line = (f" {box} {human(c.size):>10}  {core.pad(display_label(c), 20)} "
                        f"{core.pad(age, 14)} {core.shorten(loc, loc_w)}")
                line = line.ljust(w - 1)
                is_cur = c is cur_c
                base = curses.A_REVERSE if is_cur else 0
                if on and not is_cur:
                    base |= curses.A_BOLD
                self.put(yy, 0, line, base)
                if not is_cur:
                    self.put(yy, 1, box, self.color(sec) | curses.A_BOLD)
                    if "★" in age:
                        self.put(yy, line.index("★"), "★", self.color("periksa") | curses.A_BOLD)

    def draw_folder(self, y: int, hh: int, w: int) -> None:
        rows = self.folder_rows()
        if not rows:
            self.put(y + 1, 3, "Belum ada data folder.", curses.A_DIM)
            return
        cand_paths = {}
        for c in self.cands:
            for p in c.paths:
                cand_paths[p] = display_label(c) if not c.grouped else RULES[c.rule].label
        total = self.res["total"] or 1
        cur = self.cur["folder"] = min(self.cur["folder"], len(rows) - 1)
        top = self.scroll("folder", cur, hh)
        self.put(y - 1, 1, f"di {tilde(self.root)}", curses.A_DIM) if y > 2 else None
        for i, (lvl, s, p) in enumerate(rows[top: top + hh]):
            idx = top + i
            frac = s / total
            name = ("  └ " if lvl else "") + os.path.basename(p)
            tag = f"  ♻ {cand_paths[p]}" if p in cand_paths else ""
            line = f" {human(s):>10}  {core.bar(frac, 16)} {frac * 100:3.0f}%  {name}"
            line = (line + tag).ljust(w - 1)
            is_cur = idx == cur
            self.put(y + i, 0, line, curses.A_REVERSE if is_cur else (0 if lvl == 0 else curses.A_DIM))
            if not is_cur:
                self.put(y + i, 13, core.bar(frac, 16), self.color("accent"))
                if tag:
                    self.put(y + i, len(line.rstrip()) - len(tag), tag, self.color("aman"))

    def draw_sistem(self, y: int, hh: int, w: int) -> None:
        yy = y
        for m in core.real_mounts()[:4]:
            try:
                du = shutil.disk_usage(m)
            except OSError:
                continue
            pct = du.used / du.total if du.total else 0
            self.put(yy, 1, f"{core.pad(m, 14)} ")
            self.put(yy, 16, core.bar(pct, 16), self.color("danger") if pct >= .9 else self.color("accent"))
            self.put(yy, 33, f" {pct * 100:3.0f}%  sisa {human(du.free)}")
            yy += 1
        yy += 1
        if self.sys_items is None:
            self.put(yy, 3, "Memeriksa apt, journal, snap, flatpak, docker …", curses.A_DIM)
            return
        if not self.sys_items:
            self.put(yy, 3, "Tidak ada sampah sistem yang berarti. Rapi!", self.color("aman") | curses.A_BOLD)
            return
        avail = hh - (yy - y)
        cur = self.cur["sistem"] = min(self.cur["sistem"], len(self.sys_items) - 1)
        top = self.scroll("sistem", cur, avail)
        for i, (size, title, cmd, _note) in enumerate(self.sys_items[top: top + avail]):
            idx = top + i
            size_s = human(size) if size is not None else "—"
            line = f" {size_s:>10}  {core.pad(title, 30)} {core.pad(cmd, max(10, w - 46))}".ljust(w - 1)
            self.put(yy + i, 0, line, curses.A_REVERSE if idx == cur else 0)

    def draw_detail(self, y: int, hh: int, w: int) -> None:
        lines: List[Tuple[str, int]] = []
        if self.view == "rek" and self.cands:
            items = self.rek_items()
            c = items[min(self.cur["rek"], len(items) - 1)]
            r = RULES[c.rule]
            sec = core.section_of(c)
            head = display_label(c)
            if sec != "info":
                head += f"  ·  {core.MODE_LABEL[core.mode_for(c, self.force)]}"
            lines.append((head, self.color(sec) | curses.A_BOLD))
            why = r.why if c.rule != "large_file" else f"{r.why}"
            for ln in textwrap.wrap(why, w - 4)[:2]:
                lines.append((ln, 0))
            if c.grouped:
                lines.append((f"{len(c.paths)} lokasi: " + ", ".join(tilde(p) for p in c.paths[:3])
                              + (" …" if len(c.paths) > 3 else ""), curses.A_DIM))
            if r.restore:
                lines.append((("$ " if sec == "info" else "Pulihkan: ") + r.restore, self.color("accent")))
        elif self.view == "folder" and self.res:
            rows = self.folder_rows()
            if rows:
                _, s, p = rows[min(self.cur["folder"], len(rows) - 1)]
                lines.append((tilde(p), curses.A_BOLD))
                lines.append((f"{human(s)}  ·  Enter: pindai folder ini  ·  o: buka di pengelola file",
                              curses.A_DIM))
        elif self.view == "sistem" and self.sys_items:
            size, title, cmd, note = self.sys_items[min(self.cur["sistem"], len(self.sys_items) - 1)]
            lines.append((title, curses.A_BOLD))
            for ln in textwrap.wrap(note, w - 4)[:2]:
                lines.append((ln, 0))
            lines.append(("$ " + cmd, self.color("accent")))
        for i, (text, attr) in enumerate(lines[:hh]):
            self.put(y + i, 2, core.shorten(text, w - 4) if i else text[: w - 4], attr)

    def draw_status(self, y: int, w: int) -> None:
        if self.msg and time.monotonic() - self.msg_time < 4:
            self.put(y, 1, self.msg[: w - 2], self.color("accent") | curses.A_BOLD)
            return
        if self.scanning:
            hint = "Esc/q batal"
        elif self.view == "rek":
            sel = self.selected()
            tot = sum(c.size for c in sel)
            left = f" Dicentang: {len(sel)} · {human(tot)} "
            self.put(y, 0, left, curses.A_REVERSE | curses.A_BOLD)
            flags = []
            if self.stale_only:
                flags.append("≥90 hari")
            if self.force == "trash":
                flags.append("semua→Tempat Sampah")
            hint = ("[" + ", ".join(flags) + "]  " if flags else "") + \
                "Spasi centang · a aman · c bersihkan · f filter · t trash · o buka · r pindai · q keluar"
            self.put(y, len(left) + 1, hint[: w - len(left) - 2], curses.A_DIM)
            return
        elif self.view == "folder":
            hint = "Enter pindai folder · ← kembali · o buka · Tab ganti tab · q keluar"
        else:
            hint = "y salin perintah · ↑↓ pilih · perintah ini butuh sudo, jalankan sendiri · q keluar"
        self.put(y, 1, hint[: w - 2], curses.A_DIM)

    # ------------------------------------------------------------------ dialog
    def modal(self, lines: List[Tuple[str, int]], keys: str = "", title: str = "") -> str:
        """Tampilkan kotak dialog; kembalikan tombol yang ditekan (huruf kecil)."""
        h, w = self.scr.getmaxyx()
        bw = min(w - 4, max(44, max((len(t) for t, _ in lines), default=0) + 6))
        visible = lines[: max(1, h - 6)]
        bh = len(visible) + 4
        win = curses.newwin(bh, bw, max(0, (h - bh) // 2), max(0, (w - bw) // 2))
        win.keypad(True)
        win.erase()
        win.box()
        if title:
            self.put(0, 2, f" {title} ", curses.A_BOLD, win)
        for i, (text, attr) in enumerate(visible):
            self.put(2 + i, 3, text[: bw - 6], attr, win)
        win.refresh()
        if keys is None:
            return ""
        while True:
            try:
                k = win.get_wch()
            except curses.error:
                continue
            k = k.lower() if isinstance(k, str) else k
            if not keys or (isinstance(k, str) and k in keys) or k == "\x1b":
                return k if isinstance(k, str) else ""

    def clean(self) -> None:
        picks = self.selected()
        if not picks:
            self.flash("Belum ada yang dicentang. Spasi untuk mencentang, a untuk semua yang aman.")
            return
        plan = [(c, core.mode_for(c, self.force)) for c in picks]
        by_mode: dict = {}
        for c, m in plan:
            by_mode[m] = by_mode.get(m, 0) + c.size
        total = sum(c.size for c in picks)
        lines = [(f"Bersihkan {len(picks)} item, total {human(total)}?", curses.A_BOLD), ("", 0)]
        for m, s in by_mode.items():
            attr = self.color("danger") if m == "delete" else self.color("periksa")
            lines.append((f"  {core.MODE_LABEL[m]:<18} {human(s):>10}", attr))
        if "trash" in by_mode:
            lines += [("", 0), ("Item ke Tempat Sampah baru melegakan disk setelah", curses.A_DIM),
                      ("Tempat Sampah dikosongkan.", curses.A_DIM)]
        lines += [("", 0), ("[y] Ya, bersihkan      [n] Batal", curses.A_BOLD)]
        if self.modal(lines, "yn", "Konfirmasi") != "y":
            self.flash("Dibatalkan, tidak ada yang diubah.")
            return

        self.modal([("Membersihkan… mohon tunggu", curses.A_BOLD)], None, "sapu")
        in_use = core.in_use_paths()
        before = shutil.disk_usage(self.root).free
        done, problems, freed = set(), [], 0
        for c, m in plan:
            for o in core.process_candidate(c, m, self.root, in_use):
                if o.status == "ok":
                    done.add(o.path)
                    freed += o.size
                else:
                    problems.append((o.status, o.path, o.msg))
        after = shutil.disk_usage(self.root).free
        self.res["total"] = max(0, self.res["total"] - freed)
        core.forget_paths(self.res, done)
        self.checked.clear()
        self.rebuild()

        res_lines = [(f"Selesai: {human(freed)} dibersihkan ({len(done)} lokasi)", self.color("aman") | curses.A_BOLD),
                     (f"Ruang kosong: {human(before)} → {human(after)}", 0)]
        if problems:
            res_lines += [("", 0), (f"{len(problems)} dilewati/gagal:", self.color("periksa") | curses.A_BOLD)]
            for st, p, msg in problems[:12]:
                mark = "✗" if st == "fail" else "–"
                res_lines.append((f" {mark} {core.shorten(tilde(p), 40)}: {msg}", curses.A_DIM))
        res_lines += [("", 0), ("Tekan tombol apa saja", curses.A_DIM)]
        self.modal(res_lines, "", "Hasil")

    # ------------------------------------------------------------------ tombol
    def move(self, delta: int) -> None:
        if self.view == "rek":
            n = len(self.rek_items())
        elif self.view == "folder":
            n = len(self.folder_rows())
        else:
            n = len(self.sys_items or [])
        if n:
            self.cur[self.view] = max(0, min(n - 1, self.cur[self.view] + delta))

    def current_path(self) -> Optional[str]:
        if self.view == "rek" and self.cands:
            items = self.rek_items()
            return items[min(self.cur["rek"], len(items) - 1)].paths[0]
        if self.view == "folder":
            rows = self.folder_rows()
            if rows:
                return rows[min(self.cur["folder"], len(rows) - 1)][2]
        return None

    def switch(self, view: str) -> None:
        self.view = view
        if view == "sistem" and self.sys_items is None and not self.sys_thread:
            self.start_sys()

    def handle(self, k) -> bool:
        """Proses satu tombol; False = keluar."""
        page = max(1, self.scr.getmaxyx()[0] - 12)
        if self.scanning:
            if k in ("q", "\x1b"):
                self.prog.cancel = True
                if not self.res:
                    if self.scan_thread:
                        self.scan_thread.join(timeout=2)
                    return False
            return True
        if k in ("q", "Q"):
            return False
        if k in ("\t", curses.KEY_BTAB):
            ids = [v for v, _ in VIEWS]
            step = -1 if k == curses.KEY_BTAB else 1
            self.switch(ids[(ids.index(self.view) + step) % len(ids)])
        elif k in ("1", "2", "3"):
            self.switch(VIEWS[int(k) - 1][0])
        elif k in (curses.KEY_UP, "k"):
            self.move(-1)
        elif k in (curses.KEY_DOWN, "j"):
            self.move(1)
        elif k == curses.KEY_PPAGE:
            self.move(-page)
        elif k == curses.KEY_NPAGE:
            self.move(page)
        elif k in (curses.KEY_HOME, "g"):
            self.move(-10 ** 6)
        elif k in (curses.KEY_END, "G"):
            self.move(10 ** 6)
        elif k == "?":
            self.modal([(ln, curses.A_BOLD if ln and not ln.startswith(" ") else 0) for ln in HELP]
                       + [("", 0), ("Tekan tombol apa saja", curses.A_DIM)], "", "Bantuan")
        elif k == "r":
            self.start_scan(self.root)
        elif k == "o":
            p = self.current_path()
            if p:
                self.flash("Membuka " + tilde(p) if open_in_file_manager(p) else "xdg-open tidak ditemukan")
        elif self.view == "rek":
            self.handle_rek(k)
        elif self.view == "folder":
            if k in ("\n", "\r", curses.KEY_ENTER, curses.KEY_RIGHT, "l"):
                p = self.current_path()
                if p and os.path.isdir(p):
                    self.history.append(self.root)
                    self.start_scan(p)
            elif k in (curses.KEY_BACKSPACE, "\x7f", "\b", curses.KEY_LEFT, "h"):
                back = self.history.pop() if self.history else (
                    os.path.dirname(self.root) if self.root != "/" else None)
                if back:
                    self.start_scan(back)
        elif self.view == "sistem" and k == "y" and self.sys_items:
            cmd = self.sys_items[min(self.cur["sistem"], len(self.sys_items) - 1)][2]
            self.flash("Perintah disalin ke clipboard" if copy_to_clipboard(cmd)
                       else "Tidak ada alat clipboard; blok teks perintah dengan mouse")
        return True

    def handle_rek(self, k) -> None:
        items = self.rek_items()
        if k == " " and items:
            c = items[min(self.cur["rek"], len(items) - 1)]
            if core.section_of(c) == "info":
                self.flash("Item INFO tidak dihapus otomatis; jalankan perintah yang tertera.")
            else:
                key = cand_key(c)
                self.checked.symmetric_difference_update({key})
                self.move(1)
        elif k == "a":
            aman = {cand_key(c) for c in items if core.section_of(c) == "aman"}
            if aman and aman <= self.checked:
                self.checked -= aman
                self.flash("Centang semua yang aman dibatalkan")
            else:
                self.checked |= aman
                self.flash(f"{len(aman)} item aman dicentang")
        elif k == "n":
            self.checked.clear()
        elif k == "f":
            self.stale_only = not self.stale_only
            self.cur["rek"] = self.top["rek"] = 0
            self.rebuild()
            self.flash(f"Filter: hanya yang tak disentuh ≥ {core.STALE_DAYS} hari" if self.stale_only
                       else "Filter dimatikan")
        elif k == "t":
            self.force = None if self.force else "trash"
            self.flash("Semua item akan dipindah ke Tempat Sampah" if self.force
                       else "Kembali ke mode bawaan (yang bisa dibuat ulang dihapus permanen)")
        elif k in ("c", curses.KEY_DC):
            self.clean()

    def run(self) -> int:
        curses.curs_set(0)
        self.scr.keypad(True)
        if curses.has_colors():
            curses.start_color()
            try:
                curses.use_default_colors()
                bg = -1
            except curses.error:
                bg = curses.COLOR_BLACK
            curses.init_pair(1, curses.COLOR_GREEN, bg)
            curses.init_pair(2, curses.COLOR_YELLOW, bg)
            curses.init_pair(3, curses.COLOR_CYAN, bg)
            curses.init_pair(4, curses.COLOR_RED, bg)
            curses.init_pair(5, curses.COLOR_BLACK, curses.COLOR_CYAN)
        while True:
            self.poll_scan()
            self.poll_sys()
            self.draw()
            busy = self.scanning or self.sys_thread is not None or \
                (self.msg and time.monotonic() - self.msg_time < 4)
            self.scr.timeout(120 if busy else -1)
            try:
                k = self.scr.get_wch()
            except curses.error:
                continue
            except KeyboardInterrupt:
                if self.prog:
                    self.prog.cancel = True
                return 0
            if k == curses.KEY_RESIZE:
                continue
            if not self.handle(k):
                if self.prog:
                    self.prog.cancel = True
                return 0


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="sapu tui", description="Tampilan terminal interaktif sapu.")
    p.add_argument("path", nargs="?", default="~", help="folder yang dipindai (default: home)")
    p.add_argument("--rescan", action="store_true", help="abaikan hasil pindaian sebelumnya")
    args = p.parse_args(argv)
    root = os.path.realpath(os.path.expanduser(args.path))
    if not os.path.isdir(root):
        print(f"Folder tidak ditemukan: {args.path}", file=sys.stderr)
        return 2
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        print("sapu tui harus dijalankan di terminal.", file=sys.stderr)
        return 2
    locale.setlocale(locale.LC_ALL, "")
    os.environ.setdefault("ESCDELAY", "25")
    return curses.wrapper(lambda scr: App(scr, root, args.rescan).run())


if __name__ == "__main__":
    sys.exit(main())
