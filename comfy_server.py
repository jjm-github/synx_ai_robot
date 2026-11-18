from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

import requests

from config import DATA_DIR, ROOT, load_config

PID_PATH = DATA_DIR / "comfyui.pid"


def _cfg() -> dict[str, Any]:
    return (load_config().get("comfy") or {})


def install_root() -> Path:
    return Path(str(_cfg().get("root") or "~/opt/ComfyUI")).expanduser().resolve()


def base_url() -> str:
    return str(_cfg().get("base_url") or "http://127.0.0.1:8199").rstrip("/")


def _host_port() -> tuple[str, int]:
    parsed = urlparse(base_url())
    return (parsed.hostname or "127.0.0.1", int(parsed.port or 8199))


def log_path() -> Path:
    raw = Path(str(_cfg().get("log_path") or "data/logs/comfyui.log")).expanduser()
    return raw if raw.is_absolute() else (ROOT / raw)


def _read_pid() -> int | None:
    try:
        pid = int(PID_PATH.read_text(encoding="utf-8").strip())
    except Exception:
        return None
    try:
        os.kill(pid, 0)
        return pid
    except Exception:
        return None


def _pid_cmdline(pid: int) -> str:
    try:
        return Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\x00", b" ").decode("utf-8", "replace")
    except Exception:
        return ""


def _api_status(timeout: float = 3.0) -> dict[str, Any]:
    try:
        r = requests.get(base_url() + "/system_stats", timeout=timeout)
        if r.status_code >= 400:
            return {"api": False}
        data = r.json()
        system = data.get("system") or {}
        devices = []
        for d in data.get("devices") or []:
            devices.append({
                "name": d.get("name"),
                "type": d.get("type"),
                "vram_total": d.get("vram_total"),
                "vram_free": d.get("vram_free"),
            })
        return {
            "api": True,
            "version": system.get("comfyui_version"),
            "python": system.get("python_version"),
            "comfy_os": system.get("os"),
            "devices": devices,
            "queue": data.get("exec_info") or {},
        }
    except Exception as exc:
        return {"api": False, "api_error": str(exc)[:200]}


def status() -> dict[str, Any]:
    root = install_root()
    pid = _read_pid()
    api = _api_status()
    python = root / str(_cfg().get("launch_python") or ".venv/bin/python")
    host, port = _host_port()
    return {
        "running": bool(api.get("api")),
        "url": base_url(),
        "host": host,
        "port": port,
        "install_root": str(root),
        "install_exists": root.is_dir(),
        "main_py": str(root / "main.py"),
        "main_exists": (root / "main.py").is_file(),
        "python": str(python),
        "python_exists": python.exists(),
        "pid": pid,
        "pid_cmdline": _pid_cmdline(pid) if pid else "",
        "log": str(log_path()),
        "auto_start": bool(_cfg().get("auto_start", True)),
        "started_at": _started_at(),
        **api,
    }


_START_TS: float | None = None


def _started_at() -> str | None:
    global _START_TS
    if not _START_TS:
        return None
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(_START_TS))


def start(wait: int | None = None,
          progress: Callable[[dict[str, Any]], None] | None = None) -> dict[str, Any]:
    global _START_TS
    current = status()
    if current.get("running"):
        return {**current, "started": False, "already_running": True}
    root = install_root()
    if not (root / "main.py").is_file():
        return {**current, "started": False,
                "error": f"ComfyUI не найден: {root / 'main.py'}. Проверь comfy.root в config.json."}

    cfg = _cfg()
    python = root / str(cfg.get("launch_python") or ".venv/bin/python")
    if not python.exists():
        return {**current, "started": False,
                "error": f"Не найден python ComfyUI: {python}"}
    host, port = _host_port()
    raw_args = cfg.get("launch_args") or ["main.py", "--listen", "{host}", "--port", "{port}"]
    args = [str(a).format(host=host, port=port) for a in raw_args]
    log = log_path()
    log.parent.mkdir(parents=True, exist_ok=True)
    if progress:
        progress({"type": "stage", "text": f"Запускаю ComfyUI: {python} {' '.join(args)}"})
    try:
        with log.open("ab") as fh:
            fh.write(f"\n=== start {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n".encode())
            fh.flush()
            proc = subprocess.Popen(
                [str(python), *args], cwd=str(root),
                stdin=subprocess.DEVNULL, stdout=fh, stderr=subprocess.STDOUT,
                start_new_session=True)
    except Exception as exc:
        return {**current, "started": False, "error": f"не удалось запустить: {exc}"}
    PID_PATH.parent.mkdir(parents=True, exist_ok=True)
    PID_PATH.write_text(str(proc.pid), encoding="utf-8")
    _START_TS = time.time()
    wait = int(wait if wait is not None else (cfg.get("start_timeout_sec") or 120))
    deadline = time.time() + max(5, wait)
    while time.time() < deadline:
        if proc.poll() is not None:
            return {**status(), "started": False,
                    "error": (f"ComfyUI завершился с кодом {proc.returncode}. "
                              f"Смотри лог: {log}")}
        if _api_status(1.0).get("api"):
            return {**status(), "started": True, "pid": proc.pid,
                    "started_in_sec": round(time.time() - _START_TS, 1)}
        time.sleep(2)
    return {**status(), "started": True, "pid": proc.pid, "still_starting": True,
            "message": f"Процесс запущен, но API ещё не отвечает. Лог: {log}"}


def ensure(wait: int | None = None,
           progress: Callable[[dict[str, Any]], None] | None = None) -> dict[str, Any]:
    if not bool(_cfg().get("auto_start", True)):
        return status()
    return start(wait=wait, progress=progress)


def stop(wait: int = 15) -> dict[str, Any]:
    pid = _read_pid()
    if not pid:
        return {**status(), "stopped": False, "message": "ComfyUI не запущен (pid не найден)"}
    cmd = _pid_cmdline(pid)
    if str(install_root()) not in cmd:
        return {**status(), "stopped": False,
                "error": f"pid {pid} не похож на ComfyUI ({cmd[:160]}); не трогаю"}
    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
    except Exception:
        try:
            os.kill(pid, signal.SIGTERM)
        except Exception as exc:
            return {**status(), "stopped": False, "error": str(exc)}
    deadline = time.time() + max(3, wait)
    while time.time() < deadline:
        if not _read_pid():
            break
        time.sleep(0.5)
    if _read_pid():
        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        except Exception:
            pass
    try:
        PID_PATH.unlink()
    except Exception:
        pass
    return {**status(), "stopped": True, "killed_pid": pid}


def info() -> dict[str, Any]:
    return {
        "install_root": str(install_root()),
        "base_url": base_url(),
        "log": str(log_path()),
        "launch": {
            "python": str(install_root() / str(_cfg().get("launch_python") or ".venv/bin/python")),
            "args": _cfg().get("launch_args"),
            "auto_start": bool(_cfg().get("auto_start", True)),
            "start_timeout_sec": _cfg().get("start_timeout_sec") or 120,
        },
    }
