#!/usr/bin/env python3
"""Synx AI Robot — native desktop window with a local FastAPI backend.

The UI is a real desktop window (pywebview), not a browser tab.  The FastAPI
server binds to localhost only and is started in a background thread.
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import platform
import socket
import subprocess
import threading
import time
import urllib.request
from pathlib import Path

import uvicorn

from config import ensure_files, load_config
from server import app


class DesktopApi:
    """Small JS bridge for native Save As / clipboard actions."""

    @staticmethod
    def _decode(data_url: str):
        import base64
        raw = data_url.split(",", 1)[-1]
        return base64.b64decode(raw)

    def save_chat_image(self, data_url: str, filename: str = "image.png"):
        try:
            import webview
            window = webview.windows[0] if webview.windows else None
            if not window:
                return {"ok": False, "error": "no window"}
            path = window.create_file_dialog(webview.SAVE_DIALOG, save_filename=filename)
            if not path:
                return {"ok": False, "cancelled": True}
            if isinstance(path, (list, tuple)):
                path = path[0]
            Path(path).write_bytes(self._decode(data_url))
            return {"ok": True, "path": str(path)}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def save_text_file(self, text: str, filename: str = "chat.md"):
        try:
            import webview
            window = webview.windows[0] if webview.windows else None
            if not window:
                return {"ok": False, "error": "no window"}
            path = window.create_file_dialog(webview.SAVE_DIALOG, save_filename=filename)
            if not path:
                return {"ok": False, "cancelled": True}
            if isinstance(path, (list, tuple)):
                path = path[0]
            Path(path).write_text(text or "", encoding="utf-8")
            return {"ok": True, "path": str(path)}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def pick_folder(self):
        try:
            import webview
            window = webview.windows[0] if webview.windows else None
            if not window:
                return {"ok": False, "error": "no window"}
            path = window.create_file_dialog(webview.OPEN_DIALOG, allow_multiple=False)
            if not path:
                return {"ok": False, "cancelled": True}
            if isinstance(path, (list, tuple)):
                path = path[0]
            return {"ok": True, "path": str(path)}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def open_url(self, url: str):
        try:
            import webbrowser
            webbrowser.open(url)
            return {"ok": True}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def open_path(self, path: str):
        try:
            target = Path(path).expanduser()
            if not target.exists():
                return {"ok": False, "error": "file not found"}
            if platform.system() == "Windows":
                os.startfile(str(target))  # type: ignore[attr-defined]
            elif platform.system() == "Darwin":
                subprocess.Popen(["open", str(target)])
            else:
                subprocess.Popen(["xdg-open", str(target)])
            return {"ok": True, "path": str(target)}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def copy_chat_image(self, data_url: str):
        raw = self._decode(data_url)
        try:
            import gi
            gi.require_version("Gtk", "3.0")
            gi.require_version("Gdk", "3.0")
            from gi.repository import Gtk, Gdk, GdkPixbuf
            import io
            loader = GdkPixbuf.PixbufLoader()
            loader.write(raw)
            loader.close()
            pixbuf = loader.get_pixbuf()
            clipboard = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
            clipboard.set_image(pixbuf)
            clipboard.store()
            return {"ok": True}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}


def free_port(start: int = 8765) -> int:
    for port in range(start, start + 100):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise RuntimeError("no free localhost port")


def wait_server(url: str, timeout: float = 15.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            with urllib.request.urlopen(url + "/api/health", timeout=1) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(0.2)
    return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--server-only", action="store_true", help="run FastAPI without opening a window")
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()

    ensure_files()
    cfg = load_config()
    port = args.port or free_port()
    url = f"http://{args.host}:{port}"

    config = uvicorn.Config(app, host=args.host, port=port, log_level="warning", access_log=False)
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, name="fastapi", daemon=True)
    thread.start()

    if args.server_only:
        wait_server(url)
        print(f"IIRobot server: {url}")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            server.should_exit = True
        return 0

    if not wait_server(url):
        raise SystemExit("FastAPI did not start")
    try:
        import webview  # pywebview
    except Exception as exc:
        raise SystemExit(
            "pywebview is not installed. Install requirements.txt or run with --server-only.\n"
            f"Original error: {exc}"
        )
    width = int(cfg.get("ui", {}).get("window_width", 1500))
    height = int(cfg.get("ui", {}).get("window_height", 960))
    webview.create_window("IIRobot", url, width=width, height=height,
                          min_size=(1100, 700), confirm_close=True, js_api=DesktopApi())
    gui = "gtk" if platform.system() == "Linux" and importlib.util.find_spec("gi") else None
    if gui:
        webview.start(gui=gui, debug=False)
    else:
        webview.start(debug=False)
    server.should_exit = True
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
