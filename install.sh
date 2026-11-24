#!/usr/bin/env bash
# Pasang sapu (CLI + TUI + GUI) untuk user ini saja — tidak menyentuh sistem kecuali Anda setuju
# memasang python3-pyqt6 lewat apt.
#
#   ./install.sh               pasang / perbarui
#   ./install.sh --uninstall   hapus
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST="${XDG_DATA_HOME:-$HOME/.local/share}/sapu"
BIN="$HOME/.local/bin"
APPS="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
DESKTOP="$APPS/sapu.desktop"

bold() { printf '\033[1m%s\033[0m\n' "$*"; }
info() { printf '  %s\n' "$*"; }
ask()  { local a; read -r -p "  $1 [Y/n] " a || a=n; [[ -z "$a" || "$a" =~ ^[YyJj] ]]; }

refresh_menu() {
  command -v update-desktop-database >/dev/null && update-desktop-database "$APPS" 2>/dev/null || true
  for k in kbuildsycoca6 kbuildsycoca5; do command -v "$k" >/dev/null && { "$k" >/dev/null 2>&1 || true; break; }; done
}

if [[ "${1:-}" == "--uninstall" ]]; then
  rm -rf "$DEST" "$BIN/sapu" "$DESKTOP" "${XDG_CACHE_HOME:-$HOME/.cache}/sapu"
  refresh_menu
  bold "sapu sudah dihapus."
  exit 0
fi

command -v python3 >/dev/null || { echo "python3 tidak ditemukan. Pasang dulu: sudo apt install python3"; exit 1; }
for f in sapu.py sapu_tui.py sapu_gui.py; do
  [[ -f "$SRC/$f" ]] || { echo "File $f tidak ada di $SRC"; exit 1; }
done

bold "1. Menyalin program ke $DEST"
mkdir -p "$DEST" "$BIN" "$APPS"
install -m 755 "$SRC/sapu.py" "$DEST/sapu.py"
install -m 644 "$SRC/sapu_tui.py" "$SRC/sapu_gui.py" "$DEST/"
[[ -f "$SRC/README.md" ]] && install -m 644 "$SRC/README.md" "$DEST/"

bold "2. Memeriksa pustaka Qt untuk GUI"
has_qt() { "$1" -c 'import PyQt6.QtWidgets' 2>/dev/null || "$1" -c 'import PySide6.QtWidgets' 2>/dev/null; }
GUI_OK=0
if has_qt python3; then
  info "Qt untuk Python sudah tersedia."
  GUI_OK=1
elif apt-cache policy python3-pyqt6 2>/dev/null | grep -q 'Candidate: [0-9]'; then
  info "GUI memakai PyQt6 dari Ubuntu (Qt sistem, jadi tema Breeze/KDE ikut terpakai)."
  if ask "Pasang sekarang dengan: sudo apt install python3-pyqt6 ?"; then
    sudo apt install -y python3-pyqt6 && GUI_OK=1
  else
    info "Lewati. Pasang nanti: sudo apt install python3-pyqt6"
  fi
fi
if [[ $GUI_OK -eq 0 && ! -x "$DEST/venv/bin/python" ]]; then
  info "python3-pyqt6 tidak tersedia di versi Ubuntu ini. Alternatif: venv berisi PySide6 (unduhan ±100 MB)."
  if ask "Buat venv PySide6 di $DEST/venv ?"; then
    if python3 -m venv "$DEST/venv" 2>/dev/null; then
      "$DEST/venv/bin/pip" install -q --upgrade pip
      "$DEST/venv/bin/pip" install -q PySide6-Essentials && GUI_OK=1
    else
      info "Modul venv belum ada. Jalankan: sudo apt install python3-venv  lalu ulangi install.sh"
    fi
  fi
elif [[ -x "$DEST/venv/bin/python" ]]; then
  GUI_OK=1
fi

bold "3. Membuat perintah 'sapu' di $BIN"
cat > "$BIN/sapu" <<EOF
#!/bin/sh
# dibuat oleh install.sh sapu
if [ "\$1" = "gui" ] && [ -x "$DEST/venv/bin/python" ]; then
  exec "$DEST/venv/bin/python" "$DEST/sapu.py" "\$@"
fi
exec python3 "$DEST/sapu.py" "\$@"
EOF
chmod 755 "$BIN/sapu"

bold "4. Menambahkan sapu ke menu aplikasi"
TERM_CMD="konsole -e"
command -v konsole >/dev/null || TERM_CMD="x-terminal-emulator -e"
cat > "$DESKTOP" <<EOF
[Desktop Entry]
Type=Application
Name=sapu
GenericName=Pembersih Penyimpanan
Comment=Cari folder besar dan bersihkan node_modules, venv, cache, dan sampah lain dengan aman
Exec=$BIN/sapu gui
Icon=user-trash-full
Terminal=false
Categories=System;Utility;Qt;KDE;
Keywords=disk;storage;clean;cleaner;penyimpanan;bersih;node_modules;venv;cache;
StartupWMClass=sapu
Actions=tui;

[Desktop Action tui]
Name=Buka tampilan terminal (TUI)
Exec=$TERM_CMD $BIN/sapu tui
EOF
chmod 644 "$DESKTOP"
refresh_menu

echo
bold "Selesai!"
info "Terminal:  sapu          (laporan)    ·  sapu clean   ·  sapu system"
info "TUI:       sapu tui"
if [[ $GUI_OK -eq 1 ]]; then
  info "GUI:       sapu gui      atau cari \"sapu\" di menu aplikasi KDE"
else
  info "GUI:       belum aktif — pasang python3-pyqt6 lalu jalankan install.sh lagi"
fi
case ":$PATH:" in
  *":$BIN:"*) ;;
  *) echo; info "Catatan: $BIN belum ada di PATH. Logout/login sekali, atau jalankan:  source ~/.profile" ;;
esac
