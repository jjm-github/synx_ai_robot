#!/usr/bin/env bash
# IIRobot — automatic installer for Ubuntu / Linux Mint.
#
# Core install: system packages, Python venv, app dependencies, desktop entry.
# Optional: local voice models (~450 MB) and local LLM models for the voice
# corrector (~3-5 GB per model, downloaded by Ollama).
#
# Run from the repository root:
#   ./install.sh                 # interactive
#   ./install.sh --yes --llm small
#   ./install.sh --llm none --no-voice-models
#   ./install.sh --llm custom --llm-models "qwen3.5:4b qwen2.5:7b-instruct-q4_K_M"
#
# After a .deb install the same script is available as `iirobot-setup`.
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FROM_DEB="no"
ASSUME_YES="no"
DRY_RUN="no"
INSTALL_SYSTEM="ask"
APT_UPDATE="yes"
VOICE_MODELS="ask"
DESKTOP="ask"
LLM_MODE="ask"
LLM_MODELS=""
OLLAMA_MODE="ask"

T_ONE_URL="https://huggingface.co/csukuangfj/sherpa-onnx-streaming-t-one-russian-2025-09-08/resolve/main"
GIGAAM_URL="https://huggingface.co/Smirnov75/GigaAM-v3-sherpa-onnx/resolve/main"

log()  { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m  ✓\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m  !\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31mОшибка:\033[0m %s\n' "$*" >&2; exit 1; }

run() {
  if [ "$DRY_RUN" = "yes" ]; then
    printf '  [dry-run] %s\n' "$*"
  else
    "$@"
  fi
}

as_root() {
  if [ "$(id -u)" -eq 0 ]; then "$@"; else sudo "$@"; fi
}

ask_yes_no() {
  # $1 — question, $2 — default (yes/no)
  local q="$1" def="$2" ans=""
  if [ "$ASSUME_YES" = "yes" ] || [ ! -t 0 ]; then
    REPLY="$def"
    return
  fi
  read -r -p "$q [${def}]: " ans || true
  ans="${ans:-$def}"
  case "${ans,,}" in
    y|yes|д|да|1) REPLY="yes" ;;
    n|no|н|нет|0) REPLY="no" ;;
    *) REPLY="$def" ;;
  esac
}

usage() {
  cat <<'EOF'
IIRobot installer (Ubuntu / Linux Mint)

Использование: ./install.sh [опции]

Основное:
  --app-dir DIR           каталог приложения (по умолчанию — каталог скрипта)
  --yes                   без вопросов (voice-модели ставятся, LLM — нет)
  --dry-run               показать действия, ничего не менять
  --skip-system           не устанавливать apt-пакеты
  --no-apt-update         не делать apt-get update
  --no-desktop            не создавать ярлык и команду iirobot
  --from-deb              режим .deb: только модели/LLM, без apt и venv

Голосовые модели (T-one + GigaAM, ~450 МБ):
  --voice-models          скачать
  --no-voice-models       не скачивать

LLM-корректор (Ollama, локально на GPU):
  --llm MODE              none | small | default | both | custom
                          small    = qwen3.5:4b (~3.1 ГБ на диске)
                          default  = qwen2.5:7b-instruct-q4_K_M (~4.4 ГБ)
                          both     = обе модели
                          custom   = список из --llm-models
  --llm-models "A B"      свои модели Ollama (через пробел или запятую)
  --with-ollama           установить Ollama, если её нет
  --no-ollama             не устанавливать Ollama

Примеры:
  ./install.sh
  ./install.sh --yes --llm small
  ./install.sh --llm both --voice-models
  ./install.sh --llm custom --llm-models "qwen3.5:4b gemma4:12b"
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    -h|--help) usage; exit 0 ;;
    --yes|-y) ASSUME_YES="yes" ;;
    --dry-run) DRY_RUN="yes" ;;
    --app-dir) APP_DIR="$2"; shift ;;
    --skip-system) INSTALL_SYSTEM="no" ;;
    --no-apt-update) APT_UPDATE="no" ;;
    --from-deb|--models-only) FROM_DEB="yes"; INSTALL_SYSTEM="no"; DESKTOP="no" ;;
    --voice-models) VOICE_MODELS="yes" ;;
    --no-voice-models) VOICE_MODELS="no" ;;
    --desktop) DESKTOP="yes" ;;
    --no-desktop) DESKTOP="no" ;;
    --with-ollama) OLLAMA_MODE="yes" ;;
    --no-ollama) OLLAMA_MODE="no" ;;
    --llm)
      [ $# -ge 2 ] || die "--llm требует значение: none|small|default|both|custom"
      LLM_MODE="$2"; shift ;;
    --llm-models)
      [ $# -ge 2 ] || die "--llm-models требует список моделей"
      LLM_MODELS="$2"; LLM_MODE="custom"; shift ;;
    *) die "неизвестная опция: $1 (см. --help)" ;;
  esac
  shift
done

APP_DIR="$(cd "$APP_DIR" && pwd)"
[ -f "$APP_DIR/main.py" ] || die "в $APP_DIR нет main.py — укажи каталог приложения через --app-dir"

# System-wide .deb keeps mutable data in the user's XDG directories.
if [ "$FROM_DEB" = "yes" ] && [ -z "${IIROBOT_DATA_DIR:-}" ]; then
  export IIROBOT_DATA_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/iirobot"
  export IIROBOT_CONFIG="${XDG_CONFIG_HOME:-$HOME/.config}/iirobot/config.json"
  export IIROBOT_BOTS="${XDG_CONFIG_HOME:-$HOME/.config}/iirobot/bots.json"
  export IIROBOT_PROMPT="${XDG_CONFIG_HOME:-$HOME/.config}/iirobot/system.md"
  export IIROBOT_MCP_SERVERS="${XDG_CONFIG_HOME:-$HOME/.config}/iirobot/mcp_servers.json"
  mkdir -p "$IIROBOT_DATA_DIR" "$(dirname "$IIROBOT_CONFIG")"
fi

if [ "$FROM_DEB" = "no" ] && [ ! -f "$APP_DIR/requirements.txt" ]; then
  die "в $APP_DIR нет requirements.txt"
fi

# ---------------------------------------------------------------- system ---
detect_packages() {
  local pkgs=(python3 python3-venv python3-pip python3-setuptools python3-gi
              python3-gi-cairo gir1.2-gtk-3.0 ffmpeg alsa-utils xdg-utils
              curl ca-certificates)
  if apt-cache show gir1.2-webkit2-4.1 >/dev/null 2>&1; then
    pkgs+=(gir1.2-webkit2-4.1)
  else
    pkgs+=(gir1.2-webkit2-4.0)
  fi
  if apt-cache show libgtk-3-0 >/dev/null 2>&1; then
    pkgs+=(libgtk-3-0)
  else
    pkgs+=(libgtk-3-0t64)
  fi
  if apt-cache show libwebkit2gtk-4.1-0 >/dev/null 2>&1; then
    pkgs+=(libwebkit2gtk-4.1-0)
  elif apt-cache show libwebkit2gtk-4.0-37 >/dev/null 2>&1; then
    pkgs+=(libwebkit2gtk-4.0-37)
  fi
  printf '%s\n' "${pkgs[@]}"
}

install_system_packages() {
  command -v apt-get >/dev/null 2>&1 || die "нужен apt-get (Ubuntu/Debian/Mint)"
  mapfile -t pkgs < <(detect_packages)
  log "Системные пакеты: ${pkgs[*]}"
  if [ "$APT_UPDATE" = "yes" ]; then
    run as_root apt-get update
  fi
  run as_root env DEBIAN_FRONTEND=noninteractive apt-get install -y "${pkgs[@]}"
}

if [ "$FROM_DEB" = "no" ]; then
  if [ "$INSTALL_SYSTEM" = "ask" ]; then
    ask_yes_no "Установить системные пакеты через apt (GTK/WebKit, ffmpeg, arecord)?" "yes"
    INSTALL_SYSTEM="$REPLY"
  fi
  if [ "$INSTALL_SYSTEM" = "yes" ]; then
    install_system_packages
    ok "Системные пакеты готовы"
  else
    warn "Системные пакеты пропущены — проверь GTK/WebKit, ffmpeg и arecord вручную"
  fi

  # ------------------------------------------------------------- venv ----
  log "Python-окружение: $APP_DIR/.venv"
  if [ "$DRY_RUN" = "no" ] && [ ! -x "$APP_DIR/.venv/bin/python" ]; then
    python3 -m venv --system-site-packages "$APP_DIR/.venv"
  fi
  run "$APP_DIR/.venv/bin/python" -m pip install --upgrade pip
  run "$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/requirements.txt"
  ok "Python-зависимости установлены"
fi

# ------------------------------------------------------------ voice data ---
voice_dir() {
  if [ -n "${IIROBOT_DATA_DIR:-}" ]; then
    printf '%s/voice/models' "${IIROBOT_DATA_DIR%/}"
  else
    printf '%s/data/voice/models' "$APP_DIR"
  fi
}

download_file() {
  local url="$1" dst="$2" tmp
  tmp="${dst}.part"
  run mkdir -p "$(dirname "$dst")"
  if [ "$DRY_RUN" = "yes" ]; then
    printf '  [dry-run] curl -L %s -> %s\n' "$url" "$dst"
    return
  fi
  curl -L --fail --retry 3 --retry-delay 2 --progress-bar -o "$tmp" "$url"
  mv -f "$tmp" "$dst"
}

install_voice_models() {
  local dir; dir="$(voice_dir)"
  log "Голосовые модели: $dir"
  local t_one="$dir/t_one" giga="$dir/gigaam_v3"

  if [ -s "$t_one/model.onnx" ] && [ -s "$t_one/tokens.txt" ]; then
    ok "T-one уже установлен"
  else
    download_file "$T_ONE_URL/model.onnx" "$t_one/model.onnx"
    download_file "$T_ONE_URL/tokens.txt" "$t_one/tokens.txt"
    ok "T-one установлен"
  fi

  if [ -s "$giga/gigaam_v3_e2e_ctc_int8.onnx" ] && [ -s "$giga/gigaam_v3_e2e_ctc_tokens.txt" ]; then
    ok "GigaAM v3 уже установлен"
  else
    download_file "$GIGAAM_URL/gigaam_v3_e2e_ctc_int8.onnx" "$giga/gigaam_v3_e2e_ctc_int8.onnx"
    download_file "$GIGAAM_URL/gigaam_v3_e2e_ctc_tokens.txt" "$giga/gigaam_v3_e2e_ctc_tokens.txt"
    ok "GigaAM v3 установлен"
  fi
}

if [ "$VOICE_MODELS" = "ask" ]; then
  ask_yes_no "Скачать голосовые модели T-one + GigaAM (~450 МБ)?" "yes"
  VOICE_MODELS="$REPLY"
fi
if [ "$VOICE_MODELS" = "yes" ]; then
  if ! install_voice_models; then
    warn "Не удалось скачать голосовые модели — повтори: ./install.sh --voice-models"
    VOICE_MODELS="no"
  fi
else
  warn "Голосовые модели пропущены — голосовой ввод будет недоступен"
fi

# ----------------------------------------------------------------- llm ----
choose_llm_models() {
  case "$LLM_MODE" in
    none) LLM_MODELS="" ;;
    small) LLM_MODELS="qwen3.5:4b" ;;
    default) LLM_MODELS="qwen2.5:7b-instruct-q4_K_M" ;;
    both) LLM_MODELS="qwen3.5:4b qwen2.5:7b-instruct-q4_K_M" ;;
    custom)
      [ -n "$LLM_MODELS" ] || die "для --llm custom нужен --llm-models"
      LLM_MODELS="${LLM_MODELS//,/ }"
      ;;
    ask)
      if [ "$ASSUME_YES" = "yes" ] || [ ! -t 0 ]; then
        LLM_MODE="none"; LLM_MODELS=""
      else
        echo
        echo "Локальный LLM-корректор речи (Ollama):"
        echo "  1) none     — не устанавливать"
        echo "  2) small    — qwen3.5:4b, ~3.1 ГБ"
        echo "  3) default  — qwen2.5:7b, ~4.4 ГБ"
        echo "  4) both     — обе модели"
        echo "  5) custom   — ввести имена моделей"
        local n=""
        read -r -p "Выбор [2]: " n || true
        case "${n:-2}" in
          1) LLM_MODE="none" ;;
          2) LLM_MODE="small" ;;
          3) LLM_MODE="default" ;;
          4) LLM_MODE="both" ;;
          5)
            read -r -p "Модели Ollama через пробел: " LLM_MODELS || true
            LLM_MODE="custom" ;;
          *) LLM_MODE="small" ;;
        esac
        choose_llm_models
      fi
      ;;
    *) die "неизвестный режим --llm: $LLM_MODE" ;;
  esac
}
choose_llm_models

configure_voice_model() {
  local model="$1" py
  if [ "$FROM_DEB" = "yes" ] || [ ! -x "$APP_DIR/.venv/bin/python" ]; then
    py="python3"
  else
    py="$APP_DIR/.venv/bin/python"
  fi
  [ -n "$model" ] || return 0
  if [ "$DRY_RUN" = "yes" ]; then
    printf '  [dry-run] настроить voice.correct.model=%s\n' "$model"
    return
  fi
  ( cd "$APP_DIR" && "$py" - "$model" <<'PY'
import sys
from config import ensure_files, load_config, save_config
ensure_files()
cfg = load_config()
voice = cfg.setdefault("voice", {})
voice["llm_correct"] = True
correct = voice.setdefault("correct", {})
correct["provider"] = "ollama"
correct["base_url"] = correct.get("base_url") or "http://127.0.0.1:11434"
correct["model"] = sys.argv[1]
correct.setdefault("keep_alive", "5m")
correct.setdefault("timeout", 30)
correct.setdefault("temperature", 0)
correct.setdefault("num_predict", 1024)
save_config(cfg)
print("voice.correct.model =", sys.argv[1])
PY
  )
}

configure_correction_off() {
  local py
  if [ "$FROM_DEB" = "yes" ] || [ ! -x "$APP_DIR/.venv/bin/python" ]; then
    py="python3"
  else
    py="$APP_DIR/.venv/bin/python"
  fi
  [ "$DRY_RUN" = "no" ] || return 0
  ( cd "$APP_DIR" && "$py" - <<'PY'
from config import ensure_files, load_config, save_config
ensure_files()
cfg = load_config()
voice = cfg.setdefault("voice", {})
voice["llm_correct"] = False
voice.setdefault("correct", {})["provider"] = "none"
save_config(cfg)
print("voice.llm_correct = False")
PY
  )
}

install_ollama() {
  command -v curl >/dev/null 2>&1 || die "для установки Ollama нужен curl"
  log "Устанавливаю Ollama..."
  if [ "$DRY_RUN" = "yes" ]; then
    printf '  [dry-run] curl https://ollama.com/install.sh | sh (root)\n'
    return 0
  fi
  local tmp; tmp="$(mktemp /tmp/iirobot-ollama.XXXXXX)"
  curl -L --fail --retry 3 -o "$tmp" https://ollama.com/install.sh
  run as_root sh "$tmp"
  rm -f "$tmp"
  if command -v systemctl >/dev/null 2>&1; then
    run as_root systemctl enable --now ollama || true
  fi
  sleep 2
}

ensure_ollama() {
  if [ "$DRY_RUN" = "yes" ]; then
    printf '  [dry-run] проверить/запустить Ollama\n'
    return 0
  fi
  if ! command -v ollama >/dev/null 2>&1; then
    if [ "$OLLAMA_MODE" = "no" ]; then
      warn "Ollama не установлена (--no-ollama) — LLM-модели пропущены"
      return 1
    fi
    install_ollama
  fi
  if ! ollama ps >/dev/null 2>&1; then
    if command -v systemctl >/dev/null 2>&1; then
      as_root systemctl enable --now ollama >/dev/null 2>&1 || true
      sleep 2
    fi
  fi
  if ! ollama ps >/dev/null 2>&1; then
    warn "Сервер Ollama не отвечает, запускаю в фоне"
    if [ "$DRY_RUN" = "no" ]; then
      mkdir -p "$HOME/.cache/iirobot"
      nohup ollama serve >"$HOME/.cache/iirobot/ollama.log" 2>&1 &
      sleep 3
    fi
  fi
  if ! ollama ps >/dev/null 2>&1; then
    warn "Ollama всё ещё не отвечает — модели не скачаны"
    return 1
  fi
  return 0
}

if [ -n "$LLM_MODELS" ]; then
  if [ "$OLLAMA_MODE" = "ask" ]; then
    ask_yes_no "Установить Ollama и скачать LLM-модели для коррекции речи?" "yes"
    OLLAMA_MODE="$REPLY"
  fi
  if [ "$OLLAMA_MODE" = "yes" ]; then
    if ensure_ollama; then
      ready=""
      # shellcheck disable=SC2086
      for model in $LLM_MODELS; do
        if ollama list 2>/dev/null | awk 'NR>1 {print $1}' | grep -qx "$model"; then
          ok "Ollama: $model уже установлена"
          ready="$ready $model"
        else
          log "Ollama: скачиваю $model"
          if run ollama pull "$model"; then
            ready="$ready $model"
          else
            warn "Не удалось скачать $model — проверь сеть и повтори: ollama pull $model"
          fi
        fi
      done
      # First available model becomes the active voice corrector.
      # shellcheck disable=SC2086
      set -- $ready
      if [ -n "${1:-}" ]; then
        configure_voice_model "$1"
        ok "LLM-корректор: $1"
      else
        warn "Ни одна LLM-модель не установлена — корректор отключён"
        configure_correction_off
      fi
    else
      configure_correction_off
    fi
  else
    warn "Ollama пропущена — LLM-корректор отключён"
    configure_correction_off
  fi
else
  log "LLM-модели не выбраны — коррекция речи будет отключена"
  configure_correction_off
fi

# ------------------------------------------------------------- desktop ---
if [ "$DESKTOP" = "ask" ]; then
  ask_yes_no "Создать ярлык IIRobot и команду iirobot?" "yes"
  DESKTOP="$REPLY"
fi
if [ "$DESKTOP" = "yes" ]; then
  log "Создаю ярлык и команду запуска"
  if [ "$DRY_RUN" = "no" ]; then
    mkdir -p "$HOME/.local/share/applications" "$HOME/.local/bin"
    cat > "$HOME/.local/share/applications/iirobot.desktop" <<EOF
[Desktop Entry]
Type=Application
Version=1.0
Name=IIRobot
Comment=IIRobot — production assistant
Exec=${APP_DIR}/run.sh
Path=${APP_DIR}
Icon=${APP_DIR}/static/app_icon.png
Terminal=false
Categories=Graphics;Utility;
StartupNotify=true
StartupWMClass=Main.py
EOF
    chmod +x "$HOME/.local/share/applications/iirobot.desktop"
    ln -sfn "$APP_DIR/run.sh" "$HOME/.local/bin/iirobot"
    command -v update-desktop-database >/dev/null 2>&1 && \
      update-desktop-database "$HOME/.local/share/applications" >/dev/null 2>&1 || true
  fi
  ok "Ярлык: ~/.local/share/applications/iirobot.desktop"
  ok "Команда: ~/.local/bin/iirobot"
fi

# -------------------------------------------------------------- summary ---
echo
log "Готово."
echo "  Приложение:      $APP_DIR"
echo "  Запуск:          ${APP_DIR}/run.sh"
if [ "$FROM_DEB" = "yes" ]; then
  echo "  Или из меню:     IIRobot"
else
  echo "  Или из меню:     IIRobot / команда iirobot"
fi
if [ "$VOICE_MODELS" = "yes" ]; then
  echo "  Голосовые модели: $(voice_dir)"
else
  echo "  Голосовые модели: не установлены"
fi
if [ -n "$LLM_MODELS" ]; then
  echo "  LLM-корректор:   ${LLM_MODELS}"
else
  echo "  LLM-корректор:   выключен"
fi
echo
echo "Настройки приложения задаются в разделе «Настройки» (AI, synx, ComfyUI, голос)."
echo "ComfyUI устанавливается отдельно — приложение только подключается к нему по URL."
