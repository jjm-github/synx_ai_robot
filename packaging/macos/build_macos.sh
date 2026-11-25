#!/usr/bin/env bash
# Собирает macOS-установщик: dist/iiRobot-<version>-macOS-<arch>.dmg
#
#   ./packaging/macos/build_macos.sh
#   VERSION=1.1.0 ./packaging/macos/build_macos.sh
#
# Требуется macOS с Python 3.12 и Xcode Command Line Tools (sips, iconutil,
# hdiutil, codesign). Скрипт ставит зависимости и PyInstaller в текущий
# интерпретатор, собирает iiRobot.app, кладёт внутрь ffmpeg/ffprobe и
# упаковывает всё в DMG с ярлыком Applications.
#
# Подпись — ad-hoc (`codesign -s -`): нотаризации нет, при первом запуске
# нужно «ПКМ → Открыть» либо `xattr -dr com.apple.quarantine /Applications/iiRobot.app`.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
APP="iiRobot"
VERSION="${VERSION:-1.0.0}"
PY="${PYTHON:-python3}"
BUILD="$ROOT/build/macos"
DIST="$ROOT/dist"
APP_PATH="$DIST/$APP.app"
ARCH_LABEL="${ARCH_LABEL:-$(uname -m)}"

log() { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
ok()  { printf '\033[1;32m  ✓\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m  !\033[0m %s\n' "$*" >&2; }
die() { printf '\033[1;31mОшибка:\033[0m %s\n' "$*" >&2; exit 1; }

case "$ARCH_LABEL" in
  arm64|aarch64) FFMPEG_ARCH="arm64" ;;
  x86_64|amd64)  FFMPEG_ARCH="x64"   ;;
  *) die "неизвестная архитектура: $ARCH_LABEL" ;;
esac

[ "$(uname -s)" = "Darwin" ] || die "собирать macOS-установщик нужно на macOS"
command -v hdiutil >/dev/null || die "нет hdiutil (Xcode Command Line Tools)"
command -v iconutil >/dev/null || die "нет iconutil (Xcode Command Line Tools)"
"$PY" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' \
  || die "нужен Python 3.10+ (сейчас: $("$PY" -V 2>&1))"

cd "$ROOT"
mkdir -p "$BUILD" "$DIST"

log "Зависимости сборки"
"$PY" -m pip install --upgrade --quiet pip
"$PY" -m pip install --quiet -r requirements.txt pyinstaller
ok "PyInstaller $("$PY" -m PyInstaller --version)"

# ------------------------------------------------------------------- icon --
ICONSET="$BUILD/AppIcon.iconset"
rm -rf "$ICONSET"
mkdir -p "$ICONSET"
for size in 16 32 128 256 512; do
  sips -z "$size" "$size" static/app_icon.png \
    --out "$ICONSET/icon_${size}x${size}.png" >/dev/null
  double=$((size * 2))
  if [ "$double" -le 512 ]; then
    sips -z "$double" "$double" static/app_icon.png \
      --out "$ICONSET/icon_${size}x${size}@2x.png" >/dev/null
  fi
done
iconutil -c icns "$ICONSET" -o "$BUILD/AppIcon.icns"
ok "Иконка: $BUILD/AppIcon.icns"

# ------------------------------------------------------------- pyinstaller --
log "Сборка $APP.app (PyInstaller, $ARCH_LABEL)"
rm -rf "$APP_PATH" "$BUILD/pyinstaller"
"$PY" -m PyInstaller \
  --noconfirm --clean \
  --windowed \
  --name "$APP" \
  --icon "$BUILD/AppIcon.icns" \
  --osx-bundle-identifier ru.synks.iirobot \
  --paths "$ROOT" \
  --distpath "$DIST" \
  --workpath "$BUILD/pyinstaller" \
  --specpath "$BUILD/pyinstaller" \
  --add-data "$ROOT/static:static" \
  --add-data "$ROOT/prompts:prompts" \
  --collect-all sherpa_onnx \
  --collect-all reportlab \
  --collect-all webview \
  --collect-all sounddevice \
  --hidden-import webview.platforms.cocoa \
  --hidden-import sounddevice \
  --hidden-import objc \
  --hidden-import Foundation \
  --hidden-import AppKit \
  --hidden-import WebKit \
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
  frozen_entry.py

[ -x "$APP_PATH/Contents/MacOS/$APP" ] || die "не появился $APP_PATH/Contents/MacOS/$APP"
ok "Bundle: $APP_PATH ($(du -sh "$APP_PATH" | cut -f1))"

# ------------------------------------------------------------------ tools --
TOOLS="$APP_PATH/Contents/MacOS/tools/ffmpeg/bin"
mkdir -p "$TOOLS"
fetch_bin() {
  # $1 — имя (ffmpeg/ffprobe), $2 — URL
  local name="$1" url="$2" dest="$TOOLS/$1"
  if curl -fsSL --retry 3 -o "$dest" "$url"; then
    chmod +x "$dest"
    if "$dest" -version >/dev/null 2>&1; then
      ok "$name: $("$dest" -version 2>/dev/null | head -1 | cut -c1-40)"
      return 0
    fi
    warn "$name скачан, но не запускается — убираю"
    rm -f "$dest"
    return 1
  fi
  warn "не удалось скачать $name"
  return 1
}

FFBASE="https://github.com/eugeneware/ffmpeg-static/releases/latest/download"
log "Кладу ffmpeg/ffprobe в бандл"
fetch_bin ffmpeg "$FFBASE/ffmpeg-darwin-$FFMPEG_ARCH" || true
fetch_bin ffprobe "$FFBASE/ffprobe-darwin-$FFMPEG_ARCH" || true
if [ ! -x "$TOOLS/ffmpeg" ]; then
  warn "ffmpeg не попал в DMG: видео-инструменты потребуют 'brew install ffmpeg'"
fi

# -------------------------------------------------------------- info.plist --
PLIST="$APP_PATH/Contents/Info.plist"
add_plist() {
  # $1 — key, $2 — type, $3 — value
  /usr/libexec/PlistBuddy -c "Set :$1 $3" "$PLIST" 2>/dev/null \
    || /usr/libexec/PlistBuddy -c "Add :$1 $2 $3" "$PLIST"
}
add_plist NSMicrophoneUsageDescription string \
  "iiRobot использует микрофон для локального голосового ввода."
add_plist NSHighResolutionCapable bool true
add_plist LSMinimumSystemVersion string 12.0
add_plist CFBundleShortVersionString string "$VERSION"
add_plist CFBundleVersion string "$VERSION"
ok "Info.plist: микрофон, версия $VERSION"

# ------------------------------------------------------------- ad-hoc sign --
log "Подпись (ad-hoc)"
codesign --force --deep --sign - "$APP_PATH" 2>/dev/null \
  || die "codesign не смог подписать бандл"
codesign --verify --deep --strict "$APP_PATH" || die "подпись не прошла проверку"
ok "Подписано (ad-hoc, без нотаризации)"

# --------------------------------------------------------------------- dmg --
STAGE="$BUILD/dmg"
DMG="$DIST/iiRobot-$VERSION-macOS-$ARCH_LABEL.dmg"
log "Сборка DMG"
rm -rf "$STAGE" "$DMG"
mkdir -p "$STAGE"
cp -R "$APP_PATH" "$STAGE/"
ln -s /Applications "$STAGE/Applications"
cat > "$STAGE/Установка.txt" <<EOF
iiRobot $VERSION для macOS ($ARCH_LABEL)

1. Перетащите iiRobot в папку Applications.
2. Первый запуск: ПКМ по iiRobot → «Открыть» → «Открыть».
   (Приложение подписано ad-hoc и не нотаризовано Apple, поэтому
   двойной клик в первый раз может быть заблокирован Gatekeeper.)
   Альтернатива в терминале:
     xattr -dr com.apple.quarantine /Applications/iiRobot.app
3. Данные и настройки: ~/Library/Application Support/iiRobot
4. При первом запуске приложение попросит доступ к микрофону.

ffmpeg/ffprobe уже внутри приложения. Для локальной озвучки/LLM можно
поставить Ollama: https://ollama.com
EOF
hdiutil create -volname "$APP" -srcfolder "$STAGE" -ov -format UDZO "$DMG" >/dev/null \
  || die "hdiutil не собрал DMG"
ok "DMG: $DMG ($(du -h "$DMG" | cut -f1))"

# ------------------------------------------------------------------ smoke --
if [ "${SMOKE:-1}" = "1" ]; then
  log "Smoke-тест: запуск backend без окна"
  LOG="$BUILD/smoke.log"
  "$APP_PATH/Contents/MacOS/$APP" --server-only --port 8791 >"$LOG" 2>&1 &
  pid=$!
  for _ in $(seq 1 40); do
    if curl -fsS "http://127.0.0.1:8791/api/health" >/dev/null 2>&1; then break; fi
    if ! kill -0 "$pid" 2>/dev/null; then break; fi
    sleep 1
  done
  if curl -fsS "http://127.0.0.1:8791/api/health" >/dev/null 2>&1; then
    ok "Backend отвечает на /api/health"
  else
    kill "$pid" 2>/dev/null || true
    tail -40 "$LOG" >&2 || true
    die "smoke-тест не прошёл (лог: $LOG)"
  fi
  kill "$pid" 2>/dev/null || true
  wait "$pid" 2>/dev/null || true
fi

echo
echo "Готово: $DMG"
