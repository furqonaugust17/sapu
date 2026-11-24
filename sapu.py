#!/usr/bin/env python3
"""
sapu — cari apa yang menghabiskan penyimpanan di Linux, lalu bersihkan
yang aman dihapus (node_modules, venv Python, hasil build, cache aplikasi, dll).

Tanpa dependensi: cukup Python 3.8+ (bawaan Ubuntu).

  sapu                        pindai folder home
  sapu ~/proyek               pindai folder tertentu
  sapu clean                  pilih & bersihkan hasil pindaian
  sapu clean --older-than 90  hanya proyek yang tak disentuh >= 90 hari
  sapu system                 cek sampah tingkat sistem (apt, journal, snap, ...)
"""
from __future__ import annotations

import argparse
import heapq
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import textwrap
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Tuple
from urllib.parse import quote

VERSION = "1.0.0"
KiB, MiB, GiB = 1024, 1024 ** 2, 1024 ** 3

HOME = os.path.realpath(os.path.expanduser("~"))
XDG_CACHE = os.path.realpath(os.environ.get("XDG_CACHE_HOME") or os.path.join(HOME, ".cache"))
XDG_DATA = os.path.realpath(os.environ.get("XDG_DATA_HOME") or os.path.join(HOME, ".local", "share"))
STATE_DIR = os.path.join(XDG_CACHE, "sapu")
LAST_SCAN = os.path.join(STATE_DIR, "last-scan.json")
SCAN_MAX_AGE = 30 * 60      # hasil pindaian dipakai ulang oleh `clean` selama 30 menit
STALE_DAYS = 90             # proyek dianggap "tidak aktif" setelah sekian hari

# --------------------------------------------------------------------------- tampilan

_COLOR = sys.stdout.isatty() and not os.environ.get("NO_COLOR") and os.environ.get("TERM") != "dumb"


def _c(code: str, s: str) -> str:
    return f"\033[{code}m{s}\033[0m" if _COLOR else s


def bold(s): return _c("1", s)
def dim(s): return _c("2", s)
def red(s): return _c("31", s)
def green(s): return _c("32", s)
def yellow(s): return _c("33", s)
def blue(s): return _c("34", s)
def cyan(s): return _c("36", s)


def term_width() -> int:
    return max(70, min(shutil.get_terminal_size((110, 24)).columns, 170))


def human(n: float) -> str:
    n = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024 or unit == "TiB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TiB"


def parse_size(text: str) -> int:
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([kmgt]?)(?:i?b)?\s*", text, re.I)
    if not m:
        raise argparse.ArgumentTypeError(f"ukuran tidak valid: {text!r} (contoh: 50M, 1G)")
    mult = {"": 1, "k": KiB, "m": MiB, "g": GiB, "t": GiB * 1024}[m.group(2).lower()]
    return int(float(m.group(1)) * mult)


def tilde(p: str) -> str:
    if p == HOME:
        return "~"
    if p.startswith(HOME + os.sep):
        return "~" + p[len(HOME):]
    return p


def shorten(s: str, width: int) -> str:
    if width <= 1:
        return ""
    return s if len(s) <= width else "…" + s[-(width - 1):]


def pad(s: str, width: int, right: bool = False) -> str:
    if len(s) > width:
        s = s[: max(0, width - 1)] + "…"
    return s.rjust(width) if right else s.ljust(width)


def bar(frac: float, width: int = 14) -> str:
    frac = max(0.0, min(1.0, frac))
    n = int(round(frac * width))
    return "█" * n + "░" * (width - n)


def ago(ts: Optional[float]) -> str:
    if ts is None:
        return "—"
    d = (time.time() - ts) / 86400
    if d < 1:
        return "hari ini"
    if d < 60:
        return f"{int(d)} hari lalu"
    if d < 730:
        return f"{int(d / 30)} bln lalu"
    return f"{d / 365:.0f} thn lalu"


def is_stale(ts: Optional[float]) -> bool:
    return ts is not None and (time.time() - ts) / 86400 >= STALE_DAYS


# --------------------------------------------------------------------------- aturan

@dataclass(frozen=True)
class Rule:
    id: str
    label: str
    safety: str               # "aman" | "periksa" | "info"
    why: str
    restore: str = ""
    mode: str = "delete"      # delete | trash | empty_trash | none
    label_n: str = ""         # format label bila ada catatan, mis. "cache: {}"
    project: bool = True      # True = relevan menampilkan "aktif terakhir" proyek
    info_min: int = 0         # ambang minimum untuk item "info"


RULES: Dict[str, Rule] = {}


def _rule(**kw) -> None:
    r = Rule(**kw)
    RULES[r.id] = r


_BALOO = "balooctl6" if shutil.which("balooctl6") else "balooctl"

# --- di dalam proyek
_rule(id="node_modules", label="node_modules", safety="aman",
      why="Dependensi JavaScript/Node.js, diunduh ulang kapan saja.",
      restore="npm install  (atau pnpm install / yarn)")
_rule(id="venv", label="venv Python", safety="aman",
      why="Virtual environment Python; paketnya bisa dipasang ulang dari requirements/pyproject di sebelahnya.",
      restore="python3 -m venv .venv && .venv/bin/pip install -r requirements.txt")
_rule(id="venv_nofile", label="venv (tanpa req)", safety="periksa", mode="trash",
      why="Virtual environment Python TANPA requirements.txt/pyproject di sebelahnya, jadi daftar paketnya bisa hilang. "
          "Simpan dulu daftarnya.",
      restore="<venv>/bin/pip freeze > requirements.txt")
_rule(id="conda_env", label="env conda", label_n="conda: {}", safety="periksa",
      why="Environment conda. Ekspor dulu bila ingin dibuat ulang.",
      restore="conda env export -n NAMA > env.yml")
_rule(id="pycache", label="__pycache__", safety="aman",
      why="Bytecode Python; dibuat ulang otomatis saat program dijalankan.")
_rule(id="py_tool_cache", label="cache tool Python", label_n="{}", safety="aman",
      why="Cache pytest/mypy/ruff/tox; dibuat ulang otomatis.")
_rule(id="ipynb_ckpt", label="checkpoint Jupyter", safety="periksa", mode="trash",
      why="Salinan cadangan otomatis notebook Jupyter.")
_rule(id="rust_target", label="target/ Rust", safety="aman",
      why="Hasil kompilasi Rust (cargo); sering berukuran GB.", restore="cargo build")
_rule(id="maven_target", label="target/ Maven", safety="aman",
      why="Hasil build Maven.", restore="mvn package")
_rule(id="gradle_build", label="build/ Gradle", safety="aman",
      why="Hasil build Gradle/Android.", restore="./gradlew build")
_rule(id="gradle_proj", label=".gradle proyek", safety="aman",
      why="Cache Gradle per proyek.", restore="otomatis saat build")
_rule(id="flutter_build", label="build/ Flutter", safety="aman",
      why="Hasil build Flutter.", restore="flutter run / flutter build")
_rule(id="dart_tool", label=".dart_tool", safety="aman",
      why="Cache Dart/Flutter per proyek.", restore="flutter pub get")
_rule(id="cmake_build", label="build/ CMake", safety="aman",
      why="Folder build CMake.", restore="cmake -B build && cmake --build build")
_rule(id="py_build", label="build Python", safety="aman",
      why="Hasil build paket Python (build/, *.egg-info).", restore="python3 -m build")
_rule(id="js_cache", label="cache build JS", label_n="{}", safety="aman",
      why="Cache/hasil build framework JS (Next, Nuxt, SvelteKit, Turbo, Parcel, dll).",
      restore="npm run dev / npm run build")
_rule(id="dist", label="dist/build", safety="periksa", mode="trash",
      why="Hasil build proyek. Biasanya bisa dibuat ulang, tapi pastikan bukan file rilis yang ingin disimpan.",
      restore="npm run build / python3 -m build")
_rule(id="composer_vendor", label="vendor/ PHP", safety="aman",
      why="Dependensi PHP (Composer).", restore="composer install")
_rule(id="elixir", label="deps Elixir", label_n="{} Elixir", safety="aman",
      why="Dependensi/hasil build Elixir.", restore="mix deps.get && mix compile")
_rule(id="terraform", label=".terraform", safety="aman",
      why="Provider & modul Terraform yang diunduh.", restore="terraform init")
_rule(id="git", label="riwayat .git", safety="info", mode="none", info_min=500 * MiB,
      why="Riwayat Git repositori ini besar. JANGAN dihapus; coba kompres.",
      restore="git gc --aggressive --prune=now")

# --- lokasi khusus di home
_rule(id="user_cache", label="cache aplikasi", label_n="cache: {}", safety="aman", project=False,
      why="Isi ~/.cache, menurut standar Linux boleh dihapus dan dibuat ulang otomatis. "
          "Tutup aplikasinya dulu (mis. browser) sebelum menghapus cache-nya.")
_rule(id="thumbnails", label="thumbnail", safety="aman", project=False,
      why="Thumbnail gambar/video (Dolphin, Gwenview); dibuat ulang otomatis.")
_rule(id="hf_cache", label="model AI (cache)", label_n="cache: {}", safety="periksa", project=False,
      why="Model/dataset AI yang diunduh (Hugging Face/PyTorch). Bisa diunduh ulang, tapi besar & lama.")
_rule(id="npm_cache", label="cache npm", safety="aman", project=False,
      why="Cache unduhan npm.", restore="npm cache clean --force")
_rule(id="pnpm_store", label="store pnpm", safety="aman", project=False,
      why="Store paket pnpm global; proyek pnpm akan mengunduh ulang.", restore="pnpm store prune (lebih halus)")
_rule(id="yarn_cache", label="cache yarn", safety="aman", project=False,
      why="Cache Yarn Berry.", restore="yarn cache clean")
_rule(id="gradle_home", label="cache Gradle", safety="aman", project=False,
      why="Cache dependensi Gradle global (~/.gradle/caches).", restore="otomatis saat build")
_rule(id="gradle_wrapper", label="Gradle wrapper", safety="aman", project=False,
      why="Distribusi Gradle yang diunduh oleh gradlew.", restore="otomatis saat ./gradlew dijalankan")
_rule(id="m2", label="repo Maven ~/.m2", safety="periksa", project=False,
      why="Repositori Maven lokal. Bisa diunduh ulang, tapi artefak hasil `mvn install` lokal ikut hilang.")
_rule(id="cargo_registry", label="cache Cargo", safety="aman", project=False,
      why="Cache crate Rust.", restore="otomatis saat cargo build")
_rule(id="go_mod", label="cache modul Go", safety="aman", project=False,
      why="Cache modul Go.", restore="go clean -modcache  (setara)")
_rule(id="nvm_version", label="Node (nvm)", label_n="Node {}", safety="periksa", project=False,
      why="Versi Node.js terpasang via nvm. Hapus versi lama yang tidak dipakai.", restore="nvm install VERSI")
_rule(id="pyenv_version", label="Python (pyenv)", label_n="Python {}", safety="periksa", project=False,
      why="Versi Python terpasang via pyenv.", restore="pyenv install VERSI")
_rule(id="rustup_toolchain", label="toolchain Rust", label_n="Rust {}", safety="periksa", project=False,
      why="Toolchain Rust terpasang via rustup.", restore="rustup toolchain install NAMA")
_rule(id="pipenv_venv", label="venv Pipenv", label_n="pipenv: {}", safety="aman", project=False,
      why="Virtual environment milik Pipenv.", restore="pipenv install (di folder proyeknya)")
_rule(id="vscode_cache", label="cache VS Code", label_n="VS Code {}", safety="aman", project=False,
      why="Cache & log VS Code/VSCodium. Tutup editornya dulu.")
_rule(id="trash", label="Tempat Sampah", safety="aman", mode="empty_trash", project=False,
      why="File yang sudah Anda buang tapi belum dikosongkan; masih memakan ruang.")
_rule(id="baloo", label="indeks Baloo KDE", safety="info", mode="none", project=False,
      why="Indeks pencarian file KDE (Baloo). Bisa sangat besar. Bersihkan lewat perintahnya, jangan dihapus manual.",
      restore=f"{_BALOO} disable && {_BALOO} purge   (aktifkan lagi: {_BALOO} enable)")
_rule(id="android_avd", label="emulator Android", safety="info", mode="none", project=False,
      why="Emulator Android (AVD). Hapus yang tak dipakai lewat Android Studio > Device Manager.")
_rule(id="ollama", label="model Ollama", safety="info", mode="none", project=False,
      why="Model Ollama yang diunduh.", restore="ollama list  lalu  ollama rm NAMA")

# --- file besar
_rule(id="large_file", label="file besar", safety="periksa", mode="trash",
      why="File pribadi berukuran besar. Pastikan tidak dibutuhkan; default dipindah ke Tempat Sampah.")


def _home(*p: str) -> str:
    return os.path.join(HOME, *p)


HOME_EXACT: Dict[str, str] = {
    _home(".npm", "_cacache"): "npm_cache",
    _home(".local", "share", "pnpm", "store"): "pnpm_store",
    _home(".yarn", "berry", "cache"): "yarn_cache",
    _home(".gradle", "caches"): "gradle_home",
    _home(".gradle", "wrapper", "dists"): "gradle_wrapper",
    _home(".m2", "repository"): "m2",
    _home(".cargo", "registry"): "cargo_registry",
    _home(".cargo", "git"): "cargo_registry",
    _home("go", "pkg", "mod"): "go_mod",
    os.path.join(XDG_DATA, "Trash"): "trash",
    os.path.join(XDG_DATA, "baloo"): "baloo",
    _home(".android", "avd"): "android_avd",
    _home(".ollama", "models"): "ollama",
}
for _ed in ("Code", "VSCodium", "Code - OSS"):
    for _sub in ("Cache", "CachedData", "CachedExtensionVSIXs", "logs"):
        HOME_EXACT[_home(".config", _ed, _sub)] = "vscode_cache"

HOME_CHILD_OF: Dict[str, str] = {
    XDG_CACHE: "user_cache",
    _home(".nvm", "versions", "node"): "nvm_version",
    _home(".pyenv", "versions"): "pyenv_version",
    _home(".rustup", "toolchains"): "rustup_toolchain",
    os.path.join(XDG_DATA, "virtualenvs"): "pipenv_venv",
}

# folder induk yang harus selalu ditelusuri, tidak pernah dianggap kandidat
NEVER_CANDIDATE = {HOME, XDG_CACHE, XDG_DATA, _home(".config"), _home(".local")}

PY_PROJECT_FILES = {"requirements.txt", "requirements-dev.txt", "pyproject.toml", "Pipfile", "Pipfile.lock",
                    "setup.py", "setup.cfg", "poetry.lock", "uv.lock", "environment.yml", "environment.yaml"}
PY_TOOL = {".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox", ".nox", ".hypothesis", ".pytype", ".pyre"}
JS_CACHES = {".next", ".nuxt", ".output", ".svelte-kit", ".angular", ".parcel-cache", ".turbo",
             ".vercel", ".expo", ".docusaurus", ".astro", ".cache"}
GRADLE_FILES = {"build.gradle", "build.gradle.kts", "settings.gradle", "settings.gradle.kts", "gradlew"}
AI_CACHES = {"huggingface", "torch"}


def classify(path: str, name: str, parent: str, siblings: set) -> Optional[Tuple[str, str]]:
    """Kembalikan (id_aturan, catatan) bila folder ini kandidat pembersihan."""
    if path in NEVER_CANDIDATE or path == STATE_DIR:
        return None
    rid = HOME_EXACT.get(path)
    if rid:
        return rid, (name if rid == "vscode_cache" else "")
    rid = HOME_CHILD_OF.get(parent)
    if rid:
        if rid == "user_cache":
            if name == "thumbnails":
                return "thumbnails", ""
            if name in AI_CACHES:
                return "hf_cache", name
        return rid, name

    if name == "node_modules":
        return "node_modules", ""
    if os.path.isfile(os.path.join(path, "pyvenv.cfg")):
        return ("venv" if siblings & PY_PROJECT_FILES else "venv_nofile"), ""
    if parent == HOME:          # langsung di bawah home: hanya pola di atas yang berlaku
        return None

    if name == "__pycache__":
        return "pycache", ""
    if name in PY_TOOL:
        return "py_tool_cache", name
    if name == ".git":
        return "git", ""
    if name == ".ipynb_checkpoints":
        return "ipynb_ckpt", ""
    if os.path.basename(parent) == "envs" and os.path.isdir(os.path.join(path, "conda-meta")):
        return "conda_env", name
    if name == "target":
        if "Cargo.toml" in siblings:
            return "rust_target", ""
        if "pom.xml" in siblings:
            return "maven_target", ""
    if name == "build":
        if siblings & {"build.gradle", "build.gradle.kts"}:
            return "gradle_build", ""
        if "pubspec.yaml" in siblings:
            return "flutter_build", ""
        if os.path.isfile(os.path.join(path, "CMakeCache.txt")):
            return "cmake_build", ""
        if siblings & {"setup.py", "pyproject.toml", "setup.cfg"}:
            return "py_build", ""
        if "package.json" in siblings:
            return "dist", ""
    if name == "dist" and siblings & {"package.json", "setup.py", "pyproject.toml"}:
        return "dist", ""
    if name.endswith(".egg-info") and siblings & {"setup.py", "pyproject.toml", "setup.cfg"}:
        return "py_build", ""
    if name == ".gradle" and siblings & GRADLE_FILES:
        return "gradle_proj", ""
    if name == ".dart_tool" and "pubspec.yaml" in siblings:
        return "dart_tool", ""
    if name in JS_CACHES and "package.json" in siblings:
        return "js_cache", name
    if name == "vendor" and "composer.json" in siblings:
        return "composer_vendor", ""
    if name in ("_build", "deps") and "mix.exs" in siblings:
        return "elixir", name
    if name == ".terraform":
        return "terraform", ""
    return None


def file_hint(path: str) -> str:
    low = path.lower()
    ext = os.path.splitext(low)[1]
    if ext in (".iso", ".img"):
        hint = "image ISO/disk"
    elif ext == ".appimage":
        hint = "AppImage"
    elif ext in (".vdi", ".vmdk", ".qcow2", ".vhd", ".vhdx"):
        hint = "disk VM"
    elif ext in (".zip", ".7z", ".rar", ".gz", ".xz", ".bz2", ".zst", ".tar", ".tgz", ".deb", ".rpm"):
        hint = "arsip/installer"
    elif ext in (".mp4", ".mkv", ".mov", ".avi", ".webm", ".flv", ".m4v"):
        hint = "video"
    elif ext == ".log" or ".log." in low:
        hint = "log"
    elif ext in (".gguf", ".safetensors", ".pt", ".pth", ".ckpt", ".onnx", ".bin"):
        hint = "model/biner"
    else:
        hint = ext.lstrip(".") or "file"
    if "/downloads/" in low or "/unduhan/" in low:
        hint += " · Unduhan"
    return hint


# --------------------------------------------------------------------------- pemindai

def file_usage(st: os.stat_result, seen: set) -> int:
    """Ukuran nyata di disk (blok), hardlink hanya dihitung sekali."""
    if st.st_nlink > 1:
        key = (st.st_dev, st.st_ino)
        if key in seen:
            return 0
        seen.add(key)
    return st.st_blocks * 512


def du_tree(path: str, dev: Optional[int] = None, seen: Optional[set] = None, on_dir=None):
    """Total pemakaian disk sebuah folder (iteratif, tidak mengikuti symlink)."""
    seen = set() if seen is None else seen
    total = n_dirs = n_files = errors = 0
    stack = [path]
    while stack:
        cur = stack.pop()
        if on_dir:
            on_dir(cur)
        try:
            with os.scandir(cur) as it:
                for e in it:
                    try:
                        st = e.stat(follow_symlinks=False)
                    except OSError:
                        errors += 1
                        continue
                    if stat.S_ISDIR(st.st_mode):
                        if dev is not None and st.st_dev != dev:
                            continue
                        total += st.st_blocks * 512
                        n_dirs += 1
                        stack.append(e.path)
                    else:
                        n_files += 1
                        total += file_usage(st, seen)
        except OSError:
            errors += 1
    return total, n_dirs, n_files, errors


class Progress:
    def __init__(self) -> None:
        self.on = sys.stderr.isatty()
        self.last = 0.0

    def tick(self, sc: "Scanner", path: str) -> None:
        if not self.on:
            return
        now = time.monotonic()
        if now - self.last < 0.1:
            return
        self.last = now
        w = term_width()
        msg = f"  Memindai… {sc.n_dirs:,} folder · {sc.n_files:,} file  "
        sys.stderr.write("\r\033[K" + msg + shorten(tilde(path), w - len(msg) - 2))
        sys.stderr.flush()

    def done(self) -> None:
        if self.on:
            sys.stderr.write("\r\033[K")
            sys.stderr.flush()


class Scanner:
    def __init__(self, root: str, *, cross_fs: bool = False, min_file: int = 100 * MiB,
                 top_files: int = 15, depth: int = 2, progress: Optional[Progress] = None):
        self.root = os.path.realpath(os.path.expanduser(root))
        st = os.stat(self.root)
        if not stat.S_ISDIR(st.st_mode):
            raise NotADirectoryError(self.root)
        self.dev = st.st_dev
        self.cross_fs = cross_fs
        self.min_file = min_file
        self.top_files = top_files
        self.depth = depth
        self.progress = progress
        self.seen: set = set()
        self.hits: List[Tuple[str, str, int, Optional[float], str]] = []
        self.files: List[Tuple[int, str, float]] = []
        self.dir_sizes: Dict[str, int] = {}
        self.n_dirs = self.n_files = self.errors = 0
        self.total = 0
        self.elapsed = 0.0
        self.skip = {STATE_DIR}
        if self.root == "/":
            self.skip |= {"/proc", "/sys", "/dev", "/run"}

    def _tick(self, path: str) -> None:
        if self.progress:
            self.progress.tick(self, path)

    def run(self) -> "Scanner":
        t0 = time.monotonic()
        st = os.stat(self.root)
        self.total = st.st_blocks * 512 + self._walk(self.root, 0)
        self.elapsed = time.monotonic() - t0
        if self.progress:
            self.progress.done()
        return self

    def _add_file(self, path: str, size: int, mtime: float) -> None:
        item = (size, path, mtime)
        if len(self.files) < self.top_files:
            heapq.heappush(self.files, item)
        elif size > self.files[0][0]:
            heapq.heapreplace(self.files, item)

    def _walk(self, path: str, depth: int) -> int:
        self.n_dirs += 1
        self._tick(path)
        try:
            with os.scandir(path) as it:
                entries = list(it)
        except OSError:
            self.errors += 1
            return 0
        names = {e.name for e in entries}
        total = 0
        newest = 0.0                      # aktivitas terakhir di folder ini (untuk proyek)
        subdirs = []
        for e in entries:
            try:
                st = e.stat(follow_symlinks=False)
            except OSError:
                self.errors += 1
                continue
            if stat.S_ISDIR(st.st_mode):
                if e.path in self.skip or (not self.cross_fs and st.st_dev != self.dev):
                    continue
                subdirs.append((e, st, classify(e.path, e.name, path, names)))
                continue
            self.n_files += 1
            use = file_usage(st, self.seen)
            total += use
            if stat.S_ISREG(st.st_mode):
                newest = max(newest, st.st_mtime)
                if use >= self.min_file:
                    self._add_file(e.path, use, st.st_mtime)
        for e, st, hit in subdirs:
            if not hit:
                newest = max(newest, st.st_mtime)
        if not newest:                    # folder hanya berisi kandidat: pakai mtime folder itu sendiri
            try:
                newest = os.stat(path).st_mtime
            except OSError:
                pass

        for e, st, hit in subdirs:
            p = e.path
            if hit:
                rid, note = hit
                size, nd, nf, err = du_tree(p, None if self.cross_fs else self.dev, self.seen,
                                            on_dir=self._tick)
                size += st.st_blocks * 512
                self.n_dirs += nd + 1
                self.n_files += nf
                self.errors += err
                last = (newest or None) if RULES[rid].project else None
                self.hits.append((rid, p, size, last, note))
            else:
                size = st.st_blocks * 512 + self._walk(p, depth + 1)
            if depth + 1 <= self.depth:
                self.dir_sizes[p] = size
            total += size
        return total


# --------------------------------------------------------------------------- hasil

@dataclass
class Candidate:
    rule: str
    paths: List[str]
    sizes: List[int]
    last_active: Optional[float] = None
    note: str = ""

    @property
    def size(self) -> int:
        return sum(self.sizes)

    @property
    def grouped(self) -> bool:
        return len(self.paths) > 1


def display_label(c: Candidate) -> str:
    r = RULES[c.rule]
    base = r.label_n.format(c.note) if (c.note and r.label_n) else r.label
    if c.rule == "large_file":
        base = c.note or r.label
    return f"{len(c.paths)}× {r.label}" if c.grouped else base


def sort_key(c: Candidate):
    order = 2 if c.rule == "large_file" else {"aman": 0, "periksa": 1, "info": 3}[RULES[c.rule].safety]
    return (order, -c.size)


def build_result(sc: Scanner, min_size: int) -> dict:
    cands: List[Candidate] = []
    small: Dict[str, list] = {}
    for rid, p, size, last, note in sc.hits:
        r = RULES[rid]
        if r.safety == "info":
            if size >= max(min_size, r.info_min):
                cands.append(Candidate(rid, [p], [size], last, note))
            continue
        if size >= min_size:
            cands.append(Candidate(rid, [p], [size], last, note))
        else:
            small.setdefault(rid, []).append((p, size, last, note))
    # item kecil dengan jenis sama digabung agar tabel tetap ringkas
    for rid, items in small.items():
        tot = sum(i[1] for i in items)
        if tot < 1 * MiB:
            continue
        if len(items) == 1:
            p, s, l, n = items[0]
            cands.append(Candidate(rid, [p], [s], l, n))
        else:
            lasts = [i[2] for i in items if i[2] is not None]
            items.sort(key=lambda i: -i[1])
            cands.append(Candidate(rid, [i[0] for i in items], [i[1] for i in items],
                                   max(lasts) if lasts else None, ""))
    for size, p, mtime in sorted(sc.files, reverse=True):
        cands.append(Candidate("large_file", [p], [size], mtime, file_hint(p)))
    cands.sort(key=sort_key)
    return {
        "version": VERSION, "root": sc.root, "time": time.time(), "elapsed": sc.elapsed,
        "total": sc.total, "n_dirs": sc.n_dirs, "n_files": sc.n_files, "errors": sc.errors,
        "min_size": min_size, "dir_sizes": sc.dir_sizes, "candidates": [asdict(c) for c in cands],
    }


def candidates_of(res: dict) -> List[Candidate]:
    return [Candidate(**d) for d in res["candidates"]]


def apply_filters(cands: List[Candidate], args) -> List[Candidate]:
    if getattr(args, "older_than", None):
        cutoff = time.time() - args.older_than * 86400
        cands = [c for c in cands if c.last_active is not None and c.last_active < cutoff]
    return cands


def save_cache(res: dict) -> None:
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        tmp = LAST_SCAN + ".tmp"
        with open(tmp, "w") as f:
            json.dump(res, f)
        os.replace(tmp, LAST_SCAN)
    except OSError:
        pass


def load_cache(root: str) -> Optional[dict]:
    try:
        with open(LAST_SCAN) as f:
            res = json.load(f)
    except (OSError, ValueError):
        return None
    if res.get("version") != VERSION or res.get("root") != root:
        return None
    if time.time() - res.get("time", 0) > SCAN_MAX_AGE:
        return None
    return res


# --------------------------------------------------------------------------- laporan

def disk_summary(path: str, label: str) -> str:
    try:
        du = shutil.disk_usage(path)
    except OSError:
        return ""
    pct = du.used / du.total * 100 if du.total else 0
    color = red if pct >= 90 else yellow if pct >= 75 else green
    return (f"  {pad(label, 14)} {color(bar(pct / 100, 20))} {pct:3.0f}%  "
            f"terpakai {human(du.used)} dari {human(du.total)} · sisa {bold(human(du.free))}")


def render_folders(res: dict, top: int) -> List[str]:
    root, total, ds = res["root"], res["total"] or 1, res["dir_sizes"]
    kids: Dict[str, List[Tuple[int, str]]] = {}
    for p, s in ds.items():
        kids.setdefault(os.path.dirname(p), []).append((s, p))
    first = sorted(kids.get(root, []), reverse=True)[:top]
    if not first:
        return []
    out = ["", bold("FOLDER TERBESAR") + dim(f"  di {tilde(root)}")]
    for i, (s, p) in enumerate(first):
        frac = s / total
        out.append(f"  {bold(pad(human(s), 10, True))}  {cyan(bar(frac, 14))} {frac * 100:3.0f}%  {tilde(p)}")
        if i < 3:
            for s2, p2 in sorted(kids.get(p, []), reverse=True)[:4]:
                if s2 < 0.05 * s:
                    break
                out.append(dim(f"  {pad(human(s2), 10, True)}  {'':14}       └ {os.path.basename(p2)}"))
    return out


def render_candidates(cands: List[Candidate], show_legend: bool = True) -> Tuple[List[str], List[Candidate]]:
    """Tabel bernomor; kembalikan (baris, daftar yang bisa dipilih sesuai nomor)."""
    w = term_width()
    loc_w = max(24, w - 4 - 11 - 20 - 15 - 3)
    out: List[str] = []
    selectable: List[Candidate] = []

    sections = [
        ("aman", green("AMAN DIHAPUS"), "bisa dibuat ulang kapan saja",
         [c for c in cands if RULES[c.rule].safety == "aman"]),
        ("periksa", yellow("PERIKSA DULU"), "kemungkinan besar aman, tapi lihat keterangannya",
         [c for c in cands if RULES[c.rule].safety == "periksa" and c.rule != "large_file"]),
        ("file", yellow("FILE BESAR"), "file pribadi, putuskan sendiri",
         [c for c in cands if c.rule == "large_file"]),
    ]
    for _, title, sub, items in sections:
        if not items:
            continue
        tot = sum(c.size for c in items)
        out += ["", f"{bold(title)} {dim('— ' + sub)}   {dim('total')} {bold(human(tot))}",
                dim(f"  {'#':>3} {'Ukuran':>10}  {pad('Jenis', 19)} {pad('Aktif terakhir', 14)} Lokasi")]
        for c in items:
            selectable.append(c)
            n = len(selectable)
            active = ago(c.last_active) + (" ★" if is_stale(c.last_active) else "")
            active_cell = pad(active, 14).replace("★", yellow("★"))
            if c.grouped:
                loc = f"{len(c.paths)} lokasi, mis. {tilde(c.paths[0])}"
            else:
                loc = tilde(c.paths[0])
            out.append(f"  {pad(str(n), 3, True)} {bold(pad(human(c.size), 10, True))}  "
                       f"{pad(display_label(c), 19)} {active_cell} {shorten(loc, loc_w)}")
        if show_legend:
            seen = []
            for c in items:
                if c.rule not in seen:
                    seen.append(c.rule)
            for rid in seen:
                r = RULES[rid]
                text = f"{r.label}: {r.why}"
                if r.restore:
                    text += f"  Pulihkan: {r.restore}"
                wrapped = textwrap.wrap(text, w - 6) or [""]
                out.append(dim("     • " + wrapped[0]))
                out += [dim("       " + ln) for ln in wrapped[1:]]

    infos = [c for c in cands if RULES[c.rule].safety == "info"]
    if infos:
        out += ["", f"{bold(blue('INFO'))} {dim('— besar, tapi dibersihkan dengan perintahnya sendiri (tidak dihapus sapu)')}"]
        for c in infos:
            r = RULES[c.rule]
            out.append(f"      {bold(pad(human(c.size), 10, True))}  {pad(display_label(c), 19)} {tilde(c.paths[0])}")
            out.append(dim(f"{'':18}{r.why}"))
            if r.restore:
                out.append(f"{'':18}{cyan('$ ' + r.restore)}")
    return out, selectable


def render_report(res: dict, args, folders: bool = True) -> None:
    cands = apply_filters(candidates_of(res), args)
    out = ["", bold("sapu") + dim(f" {VERSION} — ringkasan penyimpanan"),
           disk_summary(res["root"], "Disk")]
    out.append(f"  {pad('Dipindai', 14)} {tilde(res['root'])}: {bold(human(res['total']))} · "
               f"{res['n_files']:,} file · {res['n_dirs']:,} folder · {res['elapsed']:.1f} dtk")
    if res["errors"]:
        out.append(dim(f"  {'':14} {res['errors']:,} item tidak bisa dibaca (izin akses) dan dilewati"))
    if getattr(args, "older_than", None):
        out.append(dim(f"  {'':14} filter: hanya yang tidak disentuh ≥ {args.older_than} hari"))
    if folders:
        out += render_folders(res, getattr(args, "top", 10))
    lines, selectable = render_candidates(cands)
    out += lines
    if not selectable and not lines:
        out += ["", green("Tidak ada kandidat pembersihan di atas ambang ukuran. Rapi!")]
    else:
        aman = sum(c.size for c in selectable if RULES[c.rule].safety == "aman")
        cek = sum(c.size for c in selectable if RULES[c.rule].safety == "periksa" and c.rule != "large_file")
        big = sum(c.size for c in selectable if c.rule == "large_file")
        out += ["", f"{bold('Potensi ruang:')} {green(human(aman))} aman · {yellow(human(cek))} perlu diperiksa · "
                    f"{human(big)} file besar"]
        if any(is_stale(c.last_active) for c in selectable):
            out.append(dim(f"★ = tidak disentuh ≥ {STALE_DAYS} hari, prioritas utama untuk dibersihkan"))
        cmd_root = "" if res["root"] == HOME else f" {tilde(res['root'])}"
        out += ["", f"Lanjut: {cyan('sapu clean' + cmd_root)}  (pilih nomor)   ·   "
                    f"sampah sistem: {cyan('sapu system')}"]
    print("\n".join(out))


# --------------------------------------------------------------------------- pembersihan

PROTECTED = {"/", "/home", "/root", "/usr", "/etc", "/var", "/boot", "/bin", "/sbin", "/lib", "/lib64",
             "/opt", "/snap", "/srv", "/mnt", "/media", "/tmp", HOME, XDG_CACHE, XDG_DATA,
             _home(".config"), _home(".local")}


def in_use_paths() -> List[str]:
    paths = []
    for var in ("VIRTUAL_ENV", "CONDA_PREFIX"):
        v = os.environ.get(var)
        if v:
            paths.append(os.path.realpath(v))
    for exe in ("node", "python3", "python", "rustc"):
        w = shutil.which(exe)
        if w:
            paths.append(os.path.realpath(w))
    return paths


def verify(p: str, rid: str, root: str, in_use: List[str]) -> Optional[str]:
    """None = aman diproses; selain itu alasan untuk melewati."""
    try:
        st = os.lstat(p)
    except FileNotFoundError:
        return "sudah tidak ada"
    except OSError as e:
        return e.strerror or str(e)
    norm = os.path.normpath(p)
    if stat.S_ISLNK(st.st_mode):
        return "berupa symlink, dilewati"
    if os.path.realpath(norm) != norm:
        return "jalurnya melewati symlink, dilewati"
    if norm in PROTECTED or len(norm.split(os.sep)) < 4:
        return "folder penting, dilindungi"
    rootp = root if root.endswith(os.sep) else root + os.sep
    if not norm.startswith(rootp):
        return "di luar folder yang dipindai"
    for u in in_use:
        if u == norm or u.startswith(norm + os.sep):
            return "sedang dipakai di sesi ini (aktif), dilewati"
    if rid == "large_file":
        return None if stat.S_ISREG(st.st_mode) else "bukan file biasa"
    if not stat.S_ISDIR(st.st_mode):
        return "bukan folder"
    parent = os.path.dirname(norm)
    try:
        sib = set(os.listdir(parent))
    except OSError as e:
        return f"tidak bisa membaca folder induk ({e.strerror})"
    hit = classify(norm, os.path.basename(norm), parent, sib)
    if not hit or hit[0] != rid:
        return "tidak lagi cocok dengan polanya, dilewati"
    return None


def rmtree_force(p: str) -> None:
    try:
        shutil.rmtree(p)
    except PermissionError:
        # contoh: cache modul Go dibuat read-only; buka izin tulis lalu coba lagi
        for dp, _dns, _fns in os.walk(p):
            try:
                os.chmod(dp, os.stat(dp).st_mode | stat.S_IRWXU)
            except OSError:
                pass
        shutil.rmtree(p)


def remove_path(p: str) -> None:
    st = os.lstat(p)
    if stat.S_ISDIR(st.st_mode):
        rmtree_force(p)
    else:
        os.unlink(p)


def empty_trash(trash_dir: str) -> None:
    for sub in ("files", "info", "expunged"):
        d = os.path.join(trash_dir, sub)
        if not os.path.isdir(d):
            continue
        for name in os.listdir(d):
            remove_path(os.path.join(d, name))
    ds = os.path.join(trash_dir, "directorysizes")
    if os.path.exists(ds):
        os.unlink(ds)


def move_to_trash(p: str) -> None:
    """Pindahkan ke Tempat Sampah sesuai standar freedesktop (terlihat di Dolphin)."""
    trash = os.path.join(XDG_DATA, "Trash")
    try:
        os.makedirs(os.path.join(trash, "files"), exist_ok=True)
        os.makedirs(os.path.join(trash, "info"), exist_ok=True)
        same_dev = os.stat(trash).st_dev == os.lstat(p).st_dev
    except OSError:
        same_dev = False
    if same_dev:
        base = os.path.basename(p)
        name, i = base, 1
        while True:
            info = os.path.join(trash, "info", name + ".trashinfo")
            if not os.path.lexists(os.path.join(trash, "files", name)):
                try:
                    fd = os.open(info, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    break
                except FileExistsError:
                    pass
            i += 1
            name = f"{base}.{i}"
        with os.fdopen(fd, "w") as f:
            f.write(f"[Trash Info]\nPath={quote(p)}\nDeletionDate={datetime.now().strftime('%Y-%m-%dT%H:%M:%S')}\n")
        try:
            os.rename(p, os.path.join(trash, "files", name))
        except OSError:
            os.unlink(info)
            raise
        return
    # beda partisi: serahkan ke gio / KDE yang tahu lokasi Trash per-partisi
    for cmd in (["gio", "trash", p], ["kioclient", "--noninteractive", "move", p, "trash:/"],
                ["kioclient5", "--noninteractive", "move", p, "trash:/"]):
        if shutil.which(cmd[0]):
            r = subprocess.run(cmd, capture_output=True, text=True)
            if r.returncode == 0 and not os.path.lexists(p):
                return
    raise RuntimeError("tidak bisa memindahkan ke Tempat Sampah (beda partisi); pakai --permanent")


MODE_LABEL = {"delete": "hapus permanen", "trash": "ke Tempat Sampah", "empty_trash": "kosongkan"}


def effective_mode(c: Candidate, args) -> str:
    return mode_for(c, "delete" if args.permanent else "trash" if args.trash else None)


def parse_selection(text: str, selectable: List[Candidate]) -> List[Candidate]:
    t = text.strip().lower()
    if t in ("", "q", "quit", "batal", "keluar"):
        return []
    if t in ("aman", "a"):
        return [c for c in selectable if RULES[c.rule].safety == "aman"]
    ids = set()
    for tok in re.split(r"[,\s]+", t):
        if not tok:
            continue
        m = re.fullmatch(r"(\d+)-(\d+)", tok)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            ids.update(range(min(a, b), max(a, b) + 1))
        elif tok.isdigit():
            ids.add(int(tok))
        else:
            raise ValueError(f"tidak paham '{tok}'")
    bad = sorted(i for i in ids if i < 1 or i > len(selectable))
    if bad:
        raise ValueError(f"nomor di luar daftar: {', '.join(map(str, bad))} (ada 1–{len(selectable)})")
    return [selectable[i - 1] for i in sorted(ids)]


class ScanCancelled(Exception):
    """Dilempar oleh objek progress (GUI/TUI) untuk menghentikan pemindaian."""


def scan_path(root: str, *, min_size: int = 10 * MiB, min_file: int = 100 * MiB, top_files: int = 15,
              cross_fs: bool = False, progress=None) -> dict:
    """API utama untuk GUI/TUI: pindai `root` dan kembalikan hasil (juga disimpan ke cache)."""
    sc = Scanner(root, cross_fs=cross_fs, min_file=min_file, top_files=top_files, progress=progress).run()
    res = build_result(sc, min_size)
    save_cache(res)
    return res


def section_of(c: Candidate) -> str:
    if c.rule == "large_file":
        return "file"
    return RULES[c.rule].safety


def mode_for(c: Candidate, force: Optional[str] = None) -> str:
    """force: None (bawaan aturan) | "trash" | "delete"."""
    m = RULES[c.rule].mode
    return m if (m == "empty_trash" or force is None) else force


@dataclass
class Outcome:
    path: str
    size: int
    status: str          # ok | skip | fail | dry
    msg: str = ""


def process_candidate(c: Candidate, mode: str, root: str, in_use: List[str],
                      dry_run: bool = False) -> List[Outcome]:
    outs: List[Outcome] = []
    for p, size in zip(c.paths, c.sizes):
        why = verify(p, c.rule, root, in_use)
        if why:
            outs.append(Outcome(p, size, "skip", why))
            continue
        if dry_run:
            outs.append(Outcome(p, size, "dry"))
            continue
        try:
            if mode == "delete":
                remove_path(p)
            elif mode == "trash":
                move_to_trash(p)
            elif mode == "empty_trash":
                empty_trash(p)
            else:
                outs.append(Outcome(p, size, "skip", "tidak bisa dihapus otomatis"))
                continue
            outs.append(Outcome(p, size, "ok"))
        except Exception as e:  # noqa: BLE001 — laporkan dan lanjutkan
            outs.append(Outcome(p, size, "fail", getattr(e, "strerror", None) or str(e)))
    return outs


def forget_paths(res: dict, done_paths: set) -> None:
    """Buang path yang sudah dibersihkan dari hasil pindaian (dan cache-nya)."""
    keep = []
    for d in res["candidates"]:
        pairs = [(p, s) for p, s in zip(d["paths"], d["sizes"]) if p not in done_paths]
        if pairs:
            d["paths"], d["sizes"] = [p for p, _ in pairs], [s for _, s in pairs]
            keep.append(d)
    res["candidates"] = keep
    save_cache(res)


def do_scan(args) -> dict:
    root = os.path.realpath(os.path.expanduser(args.path))
    print(dim(f"Memindai {tilde(root)} …"), file=sys.stderr)
    return scan_path(root, min_size=args.min_size, min_file=args.min_file, top_files=args.top_files,
                     cross_fs=args.cross_fs, progress=Progress())


def cmd_scan(args) -> int:
    res = do_scan(args)
    if args.json:
        pub = dict(res)
        pub["candidates"] = []
        for c in apply_filters(candidates_of(res), args):
            d = asdict(c)
            r = RULES[c.rule]
            d.update(size=c.size, label=display_label(c), safety=r.safety, why=r.why,
                     restore=r.restore, mode=r.mode)
            pub["candidates"].append(d)
        print(json.dumps(pub, indent=2, ensure_ascii=False))
    else:
        render_report(res, args)
    return 0


def cmd_clean(args) -> int:
    if args.trash and args.permanent:
        print(red("Pilih salah satu: --trash atau --permanent."), file=sys.stderr)
        return 2
    root = os.path.realpath(os.path.expanduser(args.path))
    res = None if args.rescan else load_cache(root)
    if res:
        mins = int((time.time() - res["time"]) / 60)
        print(dim(f"Memakai hasil pindaian {mins} menit lalu (--rescan untuk memindai ulang)."))
    else:
        res = do_scan(args)

    cands = apply_filters(candidates_of(res), args)
    lines, selectable = render_candidates(cands, show_legend=not args.select)
    if not selectable:
        print("\n".join(lines))
        print(green("\nTidak ada yang perlu dibersihkan dengan kriteria ini."))
        return 0

    if args.select:
        try:
            picks = parse_selection(args.select, selectable)
        except ValueError as e:
            print(red(f"Pilihan tidak valid: {e}"), file=sys.stderr)
            return 2
    else:
        print("\n".join(lines))
        if not sys.stdin.isatty():
            print(red("\nMode interaktif butuh terminal. Pakai --select, mis. --select '1-3 5'."), file=sys.stderr)
            return 2
        print("\n" + dim("Contoh: 1 3 5-8  ·  'aman' = semua yang aman  ·  q = batal"))
        while True:
            try:
                picks = parse_selection(input(bold("Pilih nomor: ")), selectable)
                break
            except ValueError as e:
                print(red(f"  {e}"))
    if not picks:
        print("Tidak ada yang dipilih. Tidak ada yang diubah.")
        return 0

    plan = [(c, effective_mode(c, args)) for c in picks]
    total = sum(c.size for c, _ in plan)
    to_trash = sum(c.size for c, m in plan if m == "trash")
    print("\n" + bold("Akan diproses:"))
    for c, m in plan:
        where = f"{len(c.paths)} lokasi" if c.grouped else tilde(c.paths[0])
        tag = red(MODE_LABEL[m]) if m == "delete" else yellow(MODE_LABEL[m])
        print(f"  {bold(pad(human(c.size), 10, True))}  {pad('[' + MODE_LABEL[m] + ']', 19).replace(MODE_LABEL[m], tag)}"
              f" {display_label(c)} · {where}")
    print(f"  {'Total':>10}  {bold(human(total))}")
    if to_trash:
        print(yellow(f"  Catatan: {human(to_trash)} dipindah ke Tempat Sampah, ruangnya baru lega setelah "
                     f"Tempat Sampah dikosongkan (atau pakai --permanent)."))

    if args.dry_run:
        print(dim("\n[simulasi] Tidak ada yang diubah."))
    elif not args.yes:
        try:
            ans = input(bold("\nLanjutkan? ketik 'ya': ")).strip().lower()
        except EOFError:
            ans = ""
        if ans not in ("ya", "y", "yes"):
            print("Dibatalkan. Tidak ada yang diubah.")
            return 0

    in_use = in_use_paths()
    before = shutil.disk_usage(root).free
    freed = failed = 0
    done_paths = set()
    print()
    for c, m in plan:
        for o in process_candidate(c, m, root, in_use, dry_run=args.dry_run):
            if o.status == "skip":
                print(f"  {yellow('–')} {tilde(o.path)}: {dim(o.msg)}")
            elif o.status == "dry":
                print(f"  {dim('·')} {MODE_LABEL[m]}: {tilde(o.path)} ({human(o.size)})")
            elif o.status == "fail":
                failed += 1
                print(f"  {red('✗')} {tilde(o.path)}: {o.msg}")
            else:
                freed += o.size
                done_paths.add(o.path)
                if not c.grouped:
                    print(f"  {green('✓')} {MODE_LABEL[m]}: {tilde(o.path)} ({human(o.size)})")
        if c.grouped and not args.dry_run:
            n = sum(1 for p in c.paths if p in done_paths)
            print(f"  {green('✓')} {MODE_LABEL[m]}: {n} folder {RULES[c.rule].label}")

    if args.dry_run:
        return 0
    after = shutil.disk_usage(root).free
    print("\n" + bold(f"Selesai. {human(freed)} diproses") +
          (red(f", {failed} gagal") if failed else "") +
          f". Ruang kosong sekarang {bold(human(after))} (sebelumnya {human(before)}).")

    # perbarui cache agar `sapu clean` berikutnya tidak menampilkan yang sudah dibersihkan
    forget_paths(res, done_paths)
    return 1 if failed else 0


# --------------------------------------------------------------------------- sistem

REAL_FS = {"ext2", "ext3", "ext4", "btrfs", "xfs", "f2fs", "vfat", "exfat", "ntfs", "ntfs3",
           "fuseblk", "zfs", "jfs", "reiserfs"}


def real_mounts() -> List[str]:
    out, seen = [], set()
    try:
        with open("/proc/mounts") as f:
            for line in f:
                dev, mnt, fstype = line.split()[:3]
                mnt = mnt.replace("\\040", " ")
                if fstype not in REAL_FS or dev in seen:
                    continue
                if mnt.startswith(("/snap", "/var/snap", "/var/lib/docker")):
                    continue
                seen.add(dev)
                out.append(mnt)
    except OSError:
        out = ["/"]
    return out or ["/"]


def run(cmd: List[str], timeout: int = 30) -> Tuple[int, str, str]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout, r.stderr
    except (OSError, subprocess.TimeoutExpired) as e:
        return -1, "", str(e)


def quick_du(path: str) -> Optional[int]:
    if not os.path.isdir(path):
        return None
    return du_tree(path)[0]


def _unit_bytes(num: str, unit: str, si: bool = False) -> int:
    base = 1000 if si else 1024
    unit = unit.upper().rstrip("B")
    power = {"": 0, "K": 1, "M": 2, "G": 3, "T": 4, "P": 5}.get(unit, 0)
    return int(float(num) * base ** power)


def system_items() -> List[Tuple[Optional[int], str, str, str]]:
    """Daftar (ukuran|None, judul, perintah, catatan) sampah tingkat sistem, terbesar dulu."""
    items: List[Tuple[Optional[int], str, str, str]] = []

    def add(size, title, cmd, note=""):
        items.append((size, title, cmd, note))

    s = quick_du("/var/cache/apt/archives")
    if s:
        add(s, "Cache paket APT", "sudo apt clean", "File .deb yang sudah terpasang; aman dihapus.")

    if shutil.which("apt-get"):
        rc, out, _ = run(["apt-get", "-s", "autoremove"], timeout=60)
        if rc == 0:
            rem = [ln.split()[1] for ln in out.splitlines() if ln.startswith("Remv ")]
            if rem:
                kern = sum(1 for n in rem if n.startswith("linux-image-"))
                note = f"{len(rem)} paket tak terpakai" + (f", termasuk {kern} kernel lama" if kern else "") + "."
                add(None, "Paket yatim & kernel lama", "sudo apt autoremove --purge", note)

    if shutil.which("journalctl"):
        rc, out, err = run(["journalctl", "--disk-usage"])
        m = re.search(r"take up ([\d.]+)\s*([BKMGTP])", out + err)
        if m:
            add(_unit_bytes(m.group(1), m.group(2)), "Log systemd (journal)",
                "sudo journalctl --vacuum-size=200M", "Log lama; menyisakan 200 MB terakhir.")

    if shutil.which("snap"):
        rc, out, _ = run(["snap", "list", "--all"])
        if rc == 0:
            tot, n = 0, 0
            for ln in out.splitlines()[1:]:
                parts = ln.split()
                if len(parts) >= 4 and "disabled" in parts[-1]:
                    n += 1
                    try:
                        tot += os.path.getsize(f"/var/lib/snapd/snaps/{parts[0]}_{parts[2]}.snap")
                    except OSError:
                        pass
            if n:
                add(tot, f"Revisi snap lama ({n})",
                    "snap list --all | awk '/disabled/{print $1, $3}' | "
                    "while read n r; do sudo snap remove \"$n\" --revision=\"$r\"; done",
                    "Snap menyimpan revisi lama. Batasi ke depan: sudo snap set system refresh.retain=2")

    if shutil.which("flatpak"):
        tot = sum(filter(None, [quick_du("/var/lib/flatpak"), quick_du(os.path.join(XDG_DATA, "flatpak"))]))
        if tot:
            add(tot, "Flatpak (total terpasang)", "flatpak uninstall --unused",
                "Ukuran total semua Flatpak; perintah ini hanya menghapus runtime yang tidak dipakai.")

    if shutil.which("docker"):
        rc, out, err = run(["docker", "system", "df"])
        if rc == 0:
            rec = 0
            for ln in out.splitlines()[1:]:
                m = re.search(r"(\d+(?:\.\d+)?)\s*([kKMGT]?B)\s*(?:\(\d+%\))?\s*$", ln)
                if m:
                    rec += _unit_bytes(m.group(1), m.group(2), si=True)
            if rec:
                add(rec, "Docker (bisa direklamasi)", "docker system prune   (lebih agresif: docker system prune -a)",
                    "Container berhenti, image menggantung, cache build. Hati-hati dengan -a dan --volumes.")
        elif "permission denied" in (out + err).lower():
            add(None, "Docker", "sudo docker system df", "Tidak punya izin membaca Docker dari user ini.")

    for path, title, cmd, note in (
        ("/var/crash", "Laporan crash", "sudo rm -f /var/crash/*", "Laporan crash Apport lama."),
        ("/var/lib/systemd/coredump", "Core dump", "sudo rm -f /var/lib/systemd/coredump/*",
         "Dump memori program yang crash."),
    ):
        s = quick_du(path)
        if s and s >= MiB:
            add(s, title, cmd, note)

    s = quick_du("/var/log")
    if s and s >= 500 * MiB:
        add(s, "Folder /var/log", "sudo find /var/log -type f -name '*.gz' -delete",
            "Log hasil rotasi (.gz). Periksa dulu file terbesar: sudo du -ah /var/log | sort -h | tail")

    s = quick_du(os.path.join(XDG_DATA, "Trash"))
    if s and s >= MiB:
        add(s, "Tempat Sampah", "sapu clean  (pilih Tempat Sampah)  atau kosongkan dari Dolphin", "")
    s = quick_du(os.path.join(XDG_DATA, "baloo"))
    if s and s >= 100 * MiB:
        add(s, "Indeks Baloo KDE", RULES["baloo"].restore, "Indeks pencarian file KDE.")

    items = [i for i in items if i[0] is None or i[0] >= MiB]
    items.sort(key=lambda i: -(i[0] if i[0] is not None else -1))
    return items


def cmd_system(args) -> int:
    print("\n" + bold("sapu system") + dim(" — sampah tingkat sistem"))
    for m in real_mounts():
        line = disk_summary(m, m)
        if line:
            print(line)
    print(dim("\nMemeriksa… (beberapa folder sistem hanya bisa dibaca root; ukurannya bisa lebih kecil dari aslinya)"))
    items = system_items()
    if not items:
        print(green("\nTidak ada sampah sistem yang berarti. Rapi!"))
        return 0
    w = term_width()
    print()
    for size, title, cmd, note in items:
        size_s = human(size) if size is not None else "—"
        print(f"  {bold(pad(size_s, 10, True))}  {bold(title)}")
        for ln in textwrap.wrap(note, w - 16):
            print(dim(f"{'':14}{ln}"))
        print(f"{'':14}{cyan('$ ' + cmd)}")
    known = sum(i[0] for i in items if i[0])
    print(f"\n{bold('Total terdeteksi:')} ±{human(known)}")
    print(dim("Perintah tidak dijalankan otomatis karena butuh sudo. Salin & jalankan yang Anda perlukan."))
    return 0


# --------------------------------------------------------------------------- CLI

COMMANDS = {"scan", "pindai", "clean", "bersihkan", "system", "sistem"}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="sapu",
        description="Cari apa yang memenuhi penyimpanan & bersihkan yang aman dihapus.",
        epilog="Contoh:  sapu  ·  sapu ~/proyek  ·  sapu clean --older-than 90  ·  sapu system  ·  "
               "sapu tui  (tampilan terminal)  ·  sapu gui  (jendela desktop)",
    )
    p.add_argument("-V", "--version", action="version", version=f"sapu {VERSION}")
    sub = p.add_subparsers(dest="cmd")

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("path", nargs="?", default="~", help="folder yang dipindai (default: home)")
    common.add_argument("--min-size", type=parse_size, default=10 * MiB, metavar="UKURAN",
                        help="kandidat lebih kecil dari ini digabung per jenis (default 10M)")
    common.add_argument("--min-file", type=parse_size, default=100 * MiB, metavar="UKURAN",
                        help="ambang 'file besar' (default 100M)")
    common.add_argument("--top-files", type=int, default=15, metavar="N", help="jumlah file besar (default 15)")
    common.add_argument("--older-than", type=int, metavar="HARI",
                        help="hanya yang proyeknya tidak disentuh ≥ HARI hari")
    common.add_argument("--cross-fs", action="store_true", help="ikut masuk ke partisi/mount lain")

    s = sub.add_parser("scan", aliases=["pindai"], parents=[common], help="pindai & tampilkan laporan")
    s.add_argument("--top", type=int, default=10, help="jumlah folder terbesar (default 10)")
    s.add_argument("--json", action="store_true", help="keluaran JSON (untuk skrip/GUI)")
    s.set_defaults(func=cmd_scan)

    c = sub.add_parser("clean", aliases=["bersihkan"], parents=[common], help="pilih & bersihkan")
    c.add_argument("--select", metavar="PILIHAN", help="pilih tanpa ditanya, mis. '1-3 5' atau 'aman'")
    c.add_argument("-y", "--yes", action="store_true", help="tanpa konfirmasi akhir")
    c.add_argument("-n", "--dry-run", action="store_true", help="simulasi, tidak mengubah apa pun")
    c.add_argument("--trash", action="store_true", help="semua ke Tempat Sampah (tidak langsung lega)")
    c.add_argument("--permanent", action="store_true", help="semua dihapus permanen, termasuk file besar")
    c.add_argument("--rescan", action="store_true", help="abaikan hasil pindaian sebelumnya")
    c.set_defaults(func=cmd_clean)

    y = sub.add_parser("system", aliases=["sistem"], help="cek sampah sistem (apt, journal, snap, docker...)")
    y.set_defaults(func=cmd_system)
    return p


def _run_ui(name: str, argv: List[str]) -> int:
    here = os.path.dirname(os.path.realpath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)
    try:
        mod = __import__(f"sapu_{name}")
    except ImportError as e:
        if getattr(e, "name", "") == f"sapu_{name}":
            print(red(f"File sapu_{name}.py tidak ditemukan di {here}."), file=sys.stderr)
            return 2
        raise
    return mod.main(argv)


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in ("tui", "gui"):
        return _run_ui(argv[0], argv[1:])
    if not argv:
        argv = ["scan"]
    elif argv[0] not in COMMANDS and argv[0] not in ("-h", "--help", "-V", "--version"):
        argv.insert(0, "scan")
    args = build_parser().parse_args(argv)
    if not getattr(args, "func", None):
        args = build_parser().parse_args(["scan"])
    if hasattr(args, "path"):
        root = os.path.realpath(os.path.expanduser(args.path))
        if not os.path.isdir(root):
            print(red(f"Folder tidak ditemukan: {args.path}"), file=sys.stderr)
            return 2
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\nDibatalkan.", file=sys.stderr)
        return 130
    except BrokenPipeError:
        return 0


if __name__ == "__main__":
    sys.exit(main())
