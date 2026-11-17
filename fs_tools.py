from __future__ import annotations

import base64
import mimetypes
import shutil
from pathlib import Path
from typing import Any

from config import load_config, save_config


def roots() -> list[str]:
    cfg = load_config()
    return [str(Path(x).expanduser().resolve()) for x in (cfg.get("filesystem", {}).get("roots") or [])]


def resolve(path: str) -> Path:
    p = Path(path).expanduser().resolve()
    for root in roots():
        r = Path(root)
        if p == r or r in p.parents:
            return p
    raise PermissionError(f"path outside allowed roots: {path}")


def list_dir(path: str = "") -> dict[str, Any]:
    if not path:
        out = []
        for r in roots():
            p = Path(r)
            if p.is_dir():
                out.append({"name": p.name, "path": str(p), "kind": "root",
                            "entries": len(list(p.iterdir()))})
        return {"roots": out}
    p = resolve(path)
    if not p.is_dir():
        raise NotADirectoryError(str(p))
    items = []
    for x in sorted(p.iterdir(), key=lambda z: (not z.is_dir(), z.name.lower()))[:500]:
        try:
            st = x.stat()
            items.append({"name": x.name, "path": str(x), "kind": "dir" if x.is_dir() else "file",
                          "size": st.st_size if x.is_file() else None})
        except Exception:
            pass
    return {"path": str(p), "entries": items}


def read_file(path: str, max_bytes: int = 300_000) -> dict[str, Any]:
    p = resolve(path)
    if not p.is_file():
        raise FileNotFoundError(str(p))
    raw = p.read_bytes()[:max_bytes]
    mime = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
    if mime.startswith("text/") or p.suffix.lower() in {".md", ".json", ".py", ".txt", ".yaml", ".yml", ".js", ".ts", ".html", ".css", ".csv", ".log"}:
        return {"path": str(p), "kind": "text", "mime": mime,
                "content": raw.decode("utf-8", "replace"), "truncated": p.stat().st_size > max_bytes}
    return {"path": str(p), "kind": "binary", "mime": mime, "size": p.stat().st_size,
            "base64": base64.b64encode(raw).decode("ascii"), "truncated": p.stat().st_size > max_bytes}


def write_file(path: str, content: str, base64_data: bool = False) -> dict[str, Any]:
    p = resolve(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    if base64_data:
        raw = base64.b64decode(content)
    else:
        raw = content.encode("utf-8")
    p.write_bytes(raw)
    return {"path": str(p), "bytes": len(raw)}


def delete_path(path: str) -> dict[str, Any]:
    p = resolve(path)
    if p.is_dir():
        shutil.rmtree(p)
    else:
        p.unlink()
    return {"path": str(p), "deleted": True}


def add_root(path: str) -> list[str]:
    p = Path(path).expanduser().resolve()
    if not p.is_dir():
        raise NotADirectoryError(str(p))
    cfg = load_config()
    cur = [str(Path(x).expanduser().resolve()) for x in (cfg.get("filesystem", {}).get("roots") or [])]
    if str(p) not in cur:
        cur.append(str(p))
    cfg.setdefault("filesystem", {})["roots"] = cur
    save_config(cfg)
    return cur


def remove_root(path: str) -> list[str]:
    cfg = load_config()
    p = str(Path(path).expanduser().resolve())
    cur = [str(Path(x).expanduser().resolve()) for x in (cfg.get("filesystem", {}).get("roots") or [])]
    cfg.setdefault("filesystem", {})["roots"] = [x for x in cur if x != p]
    save_config(cfg)
    return cfg["filesystem"]["roots"]
