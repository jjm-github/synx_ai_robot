#!/usr/bin/env bash
# Собирает один исполняемый файл для Linux (PyInstaller).
#
#   ./packaging/build_bin.sh                # dist/iirobot (one-file)
#   MODE=onedir ./packaging/build_bin.sh    # dist/iirobot/ (каталог)
#
# Требует .venv с pyinstaller:  .venv/bin/python -m pip install pyinstaller
#
# GTK/WebKit (PyGObject) в архив НЕ попадают: импортируются из системного
# Python, а библиотеки ставятся пакетами ОС (см. frozen_entry.py).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP="iirobot"
MODE="${MODE:-onefile}"
PY="${PYTHON:-$ROOT/.venv/bin/python}"
DIST="$ROOT/dist"
WORK="$ROOT/build/pyinstaller"

log() { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
ok()  { printf '\033[1;32m  ✓\033[0m %s\n' "$*"; }
die() { printf '\033[1;31mОшибка:\033[0m %s\n' "$*" >&2; exit 1; }

[ -x "$PY" ] || die "нет интерпретатора $PY (создай .venv)"
"$PY" -c "import PyInstaller" 2>/dev/null || \
  die "pyinstaller не установлен: $PY -m pip install pyinstaller"

cd "$ROOT"

case "$MODE" in
  onefile) MODE_FLAG=(--onefile) ;;
  onedir)  MODE_FLAG=(--onedir)  ;;
  *)       die "MODE должен быть onefile или onedir, получено: $MODE" ;;
esac

log "Сборка PyInstaller ($MODE): $APP"
"$PY" -m PyInstaller \
  --noconfirm --clean \
  --name "$APP" \
  --paths "$ROOT" \
  --distpath "$DIST" \
  --workpath "$WORK" \
  --specpath "$WORK" \
  --add-data "$ROOT/static:static" \
  --add-data "$ROOT/prompts:prompts" \
  --collect-all sherpa_onnx \
  --collect-all reportlab \
  --hidden-import webview.platforms.gtk \
  --hidden-import optparse \
  --hidden-import fcntl \
  --exclude-module gi \
  --exclude-module GObject \
  --exclude-module Gtk \
  --exclude-module Gdk \
  --exclude-module GdkPixbuf \
  --exclude-module WebKit2 \
  --exclude-module cairo \
  --exclude-module tkinter \
  --exclude-module PyQt5 \
  --exclude-module PyQt6 \
  --exclude-module PySide2 \
  --exclude-module PySide6 \
  --exclude-module matplotlib \
  "${MODE_FLAG[@]}" \
  frozen_entry.py

BIN="$DIST/$APP"
if [ "$MODE" = "onefile" ]; then
  [ -x "$BIN" ] || die "бинарник не появился: $BIN"
  chmod +x "$BIN"
  ok "Собрано: $BIN ($(du -h "$BIN" | cut -f1))"
else
  [ -x "$BIN/$APP" ] || die "бинарник не появился: $BIN/$APP"
  ok "Собрано: $BIN/$APP ($(du -sh "$BIN" | cut -f1))"
fi

echo
echo "Запуск:"
echo "  $BIN"
echo "  $BIN --server-only      # без окна, только FastAPI"
echo
echo "Системные зависимости (GTK/WebKit, ffmpeg, arecord):"
echo "  sudo apt install gir1.2-gtk-3.0 gir1.2-webkit2-4.1 ffmpeg alsa-utils"
