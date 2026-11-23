#!/usr/bin/env python3
"""Точка входа для замороженного (PyInstaller) приложения.

Собирается для Linux (one-file) и macOS (.app, см. packaging/macos).

Отличия от `python main.py`:

1. Мутабельные данные уходят в пользовательские каталоги:
   `~/.local/share/iirobot` / `~/.config/iirobot` (Linux, XDG) либо
   `~/Library/Application Support/IIRobot` (macOS) — внутри бандла писать
   нельзя.
2. GTK/WebKit (PyGObject) намеренно НЕ запаковываются в Linux-бинарник: в
   `sys.path` добавляется dist-packages системного Python, откуда берётся
   `gi`, а сами библиотеки GTK/WebKit ставятся пакетами ОС.
   На macOS pywebview работает через PyObjC и пакуется целиком.
3. `IIRobot --iirobot-module passport_batch ...` — перезапуск самого себя
   для отвязанных фоновых задач (PyInstaller кладёт модули в PYZ, а не в
   файлы .py, поэтому обычный `sys.executable <script.py>` не работает).
4. Во bundled-каталоге `tools/` (рядом с исполняемым файлом) ищутся
   ffmpeg/ffprobe/poppler; их путь попадает в `PATH` и `IIROBOT_TOOLS_DIR`.
"""
from __future__ import annotations

import glob
import importlib
import os
import sys
from pathlib import Path

MODULE_FLAG = "--iirobot-module"


def _user_dirs() -> tuple[Path, Path]:
    home = Path.home()
    if sys.platform == "darwin":
        base = home / "Library" / "Application Support" / "IIRobot"
        return base, base
    xdg_data = Path(os.environ.get("XDG_DATA_HOME") or (home / ".local" / "share"))
    xdg_conf = Path(os.environ.get("XDG_CONFIG_HOME") or (home / ".config"))
    return xdg_data / "iirobot", xdg_conf / "iirobot"


def setup_frozen() -> Path:
    """Пользовательские пути + доступ к gi системного Python (Linux)."""
    data, conf = _user_dirs()
    data.mkdir(parents=True, exist_ok=True)
    conf.mkdir(parents=True, exist_ok=True)

    os.environ.setdefault("IIROBOT_DATA_DIR", str(data))
    os.environ.setdefault("IIROBOT_CONFIG", str(conf / "config.json"))
    os.environ.setdefault("IIROBOT_BOTS", str(conf / "bots.json"))
    os.environ.setdefault("IIROBOT_PROMPT", str(conf / "system.md"))
    os.environ.setdefault("IIROBOT_WORKFLOWS", str(data / "workflows"))
    os.environ.setdefault("IIROBOT_MCP_SERVERS", str(conf / "mcp_servers.json"))

    tools = Path(sys.executable).resolve().parent / "tools"
    if tools.is_dir():
        os.environ.setdefault("IIROBOT_TOOLS_DIR", str(tools))

    if sys.platform != "darwin":
        # В конце пути, чтобы модули из архива имели приоритет.
        for pattern in ("/usr/lib/python3/dist-packages",
                        "/usr/lib/python3.*/dist-packages",
                        "/usr/local/lib/python3.*/dist-packages"):
            for path in sorted(glob.glob(pattern), reverse=True):
                if os.path.isdir(path) and path not in sys.path:
                    sys.path.append(path)

    _redirect_streams(data)
    return data


def _redirect_streams(data: Path) -> None:
    """В windowed-сборках stdout/stderr = None, а `print()` тогда падает."""
    if sys.stdout is not None and sys.stderr is not None:
        return
    try:
        log = open(data / "iirobot.log", "a", buffering=1,
                  encoding="utf-8", errors="replace")
    except OSError:
        return
    if sys.stdout is None:
        sys.stdout = log
    if sys.stderr is None:
        sys.stderr = log


def run_module(name: str) -> int:
    module = importlib.import_module(name)
    main = getattr(module, "main", None)
    if main is None:
        raise SystemExit(f"модуль {name} не имеет main()")
    return int(main() or 0)


def main() -> int:
    if getattr(sys, "frozen", False):
        setup_frozen()
        if len(sys.argv) > 2 and sys.argv[1] == MODULE_FLAG:
            name = sys.argv[2]
            sys.argv = [sys.argv[0], *sys.argv[3:]]
            return run_module(name)
    import main as app_main  # noqa: PLC0415 — только после setup_frozen()
    return int(app_main.main() or 0)


if __name__ == "__main__":
    raise SystemExit(main())
