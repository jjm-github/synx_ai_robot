#!/usr/bin/env bash
# Build an installable .deb for Ubuntu / Linux Mint.
#
#   ./packaging/build_deb.sh
#   VERSION=1.2.0 ./packaging/build_deb.sh
#
# The package installs the app into /opt/iirobot and Python wheels into
# /opt/iirobot/wheels, so the post-install step does not need the network.
# User data/config lives in ~/.local/share/iirobot and ~/.config/iirobot.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VERSION="${VERSION:-1.0.0}"
ARCH="$(dpkg --print-architecture)"
PYVER="$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
NEXTVER="$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor + 1}")')"

APP="iirobot"
PKGROOT="$ROOT/build/deb/${APP}"
DIST="$ROOT/dist"
WHEELS="$PKGROOT/opt/iirobot/wheels"

log()  { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m  ✓\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31mОшибка:\033[0m %s\n' "$*" >&2; exit 1; }

command -v dpkg-deb >/dev/null 2>&1 || die "нужен dpkg-deb (пакет dpkg-dev)"
command -v python3 >/dev/null 2>&1 || die "нужен python3"

rm -rf "$ROOT/build/deb"
mkdir -p "$PKGROOT/opt/iirobot" "$PKGROOT/DEBIAN" \
         "$PKGROOT/usr/bin" "$PKGROOT/usr/share/applications" \
         "$PKGROOT/usr/share/icons/hicolor/512x512/apps" "$DIST"

log "Копирую приложение в $PKGROOT/opt/iirobot"
tar -C "$ROOT" \
  --exclude=.git --exclude=.venv --exclude=__pycache__ --exclude='*.pyc' \
  --exclude=data --exclude=tmp --exclude=build --exclude=dist \
  --exclude=config.json --exclude=bots.json --exclude=mcp_servers.json \
  --exclude=.pytest_cache \
  -cf - . | tar -C "$PKGROOT/opt/iirobot" -xf -
chmod +x "$PKGROOT/opt/iirobot/run.sh" "$PKGROOT/opt/iirobot/install.sh" 2>/dev/null || true

log "Скачиваю Python wheels (offline install)"
mkdir -p "$WHEELS"
python3 -m pip download -r "$ROOT/requirements.txt" -d "$WHEELS" >/dev/null
# proxy-tools is published only as an sdist; build it into a wheel so that
# postinst can use --no-index without build isolation.
python3 -m pip wheel --no-deps -w "$WHEELS" proxy-tools==0.1.0 >/dev/null
rm -f "$WHEELS"/proxy_tools-*.tar.gz
ok "Wheels: $(du -sh "$WHEELS" | cut -f1)"

# ---------------------------------------------------------------- launcher --
cat > "$PKGROOT/usr/bin/iirobot" <<'EOF'
#!/bin/sh
# IIRobot launcher: keep mutable data in the user's XDG directories.
APP=/opt/iirobot
DATA="${XDG_DATA_HOME:-$HOME/.local/share}/iirobot"
CONF="${XDG_CONFIG_HOME:-$HOME/.config}/iirobot"
mkdir -p "$DATA" "$CONF" 2>/dev/null || true
export IIROBOT_DATA_DIR="$DATA"
export IIROBOT_CONFIG="$CONF/config.json"
export IIROBOT_BOTS="$CONF/bots.json"
export IIROBOT_PROMPT="$CONF/system.md"
export IIROBOT_MCP_SERVERS="$CONF/mcp_servers.json"
exec "$APP/.venv/bin/python" "$APP/main.py" "$@"
EOF
chmod 755 "$PKGROOT/usr/bin/iirobot"

cat > "$PKGROOT/usr/bin/iirobot-setup" <<'EOF'
#!/bin/sh
# Add voice models / local LLM models to an installed IIRobot.
# Run WITHOUT sudo: the script asks for sudo only when it needs apt/Ollama.
APP=/opt/iirobot
DATA="${XDG_DATA_HOME:-$HOME/.local/share}/iirobot"
CONF="${XDG_CONFIG_HOME:-$HOME/.config}/iirobot"
mkdir -p "$DATA" "$CONF" 2>/dev/null || true
export IIROBOT_DATA_DIR="$DATA"
export IIROBOT_CONFIG="$CONF/config.json"
export IIROBOT_BOTS="$CONF/bots.json"
export IIROBOT_PROMPT="$CONF/system.md"
export IIROBOT_MCP_SERVERS="$CONF/mcp_servers.json"
exec "$APP/install.sh" --from-deb --app-dir "$APP" "$@"
EOF
chmod 755 "$PKGROOT/usr/bin/iirobot-setup"

cat > "$PKGROOT/usr/share/applications/iirobot.desktop" <<'EOF'
[Desktop Entry]
Type=Application
Version=1.0
Name=IIRobot
Comment=IIRobot — production assistant
Exec=/usr/bin/iirobot
Icon=iirobot
Terminal=false
Categories=Graphics;Utility;
StartupNotify=true
StartupWMClass=Main.py
EOF

cp "$ROOT/static/app_icon.png" "$PKGROOT/usr/share/icons/hicolor/512x512/apps/iirobot.png"

# ----------------------------------------------------------------- control --
INSTALLED_SIZE="$(du -sk "$PKGROOT" | cut -f1)"
cat > "$PKGROOT/DEBIAN/control" <<EOF
Package: ${APP}
Version: ${VERSION}
Section: graphics
Priority: optional
Architecture: ${ARCH}
Installed-Size: ${INSTALLED_SIZE}
Maintainer: IIRobot <iirobot@localhost>
Depends: python3 (>= ${PYVER}), python3 (<< ${NEXTVER}), python3-venv, python3-setuptools, python3-pip, python3-gi, gir1.2-gtk-3.0, gir1.2-webkit2-4.1 | gir1.2-webkit2-4.0, libgtk-3-0 | libgtk-3-0t64, libwebkit2gtk-4.1-0 | libwebkit2gtk-4.0-37, ffmpeg, alsa-utils, xdg-utils, ca-certificates
Description: IIRobot production assistant
 Native desktop assistant for synx production: chat, tasks, ComfyUI and
 local voice input. Installs the application into /opt/iirobot and keeps
 user data in ~/.local/share/iirobot and ~/.config/iirobot.
EOF

cat > "$PKGROOT/DEBIAN/postinst" <<'EOF'
#!/bin/sh
set -e
APP=/opt/iirobot

if [ ! -x "$APP/.venv/bin/python" ]; then
    python3 -m venv --system-site-packages "$APP/.venv" || true
fi
if [ -x "$APP/.venv/bin/python" ]; then
    if ! "$APP/.venv/bin/python" - <<'PY' >/dev/null 2>&1
import fastapi, uvicorn, webview, sherpa_onnx, PIL, reportlab
PY
    then
        "$APP/.venv/bin/pip" install --no-index --find-links "$APP/wheels" \
            -r "$APP/requirements.txt" || \
        "$APP/.venv/bin/pip" install -r "$APP/requirements.txt" || \
        echo "IIRobot: не удалось установить Python-зависимости; повтори: $APP/.venv/bin/pip install -r $APP/requirements.txt" >&2
    fi
fi
chmod +x /usr/bin/iirobot /usr/bin/iirobot-setup "$APP/run.sh" "$APP/install.sh" 2>/dev/null || true
if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database -q || true
fi
exit 0
EOF
chmod 755 "$PKGROOT/DEBIAN/postinst"

cat > "$PKGROOT/DEBIAN/postrm" <<'EOF'
#!/bin/sh
set -e
if [ "$1" = "purge" ]; then
    rm -rf /opt/iirobot/.venv
fi
exit 0
EOF
chmod 755 "$PKGROOT/DEBIAN/postrm"

# ------------------------------------------------------------------- build --
OUT="$DIST/${APP}_${VERSION}_${ARCH}.deb"
dpkg-deb --build --root-owner-group "$PKGROOT" "$OUT" >/dev/null
ok "Собрано: $OUT ($(du -h "$OUT" | cut -f1))"
echo
echo "Установка:"
echo "  sudo apt install ./$(basename "$OUT")"
echo "  # или: sudo dpkg -i $OUT && sudo apt -f install"
echo
echo "После установки (без sudo):"
echo "  iirobot-setup --voice-models --llm small"
