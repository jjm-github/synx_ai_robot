from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Any

import requests

from config import DATA_DIR, ROOT

PATH = Path(os.environ.get("IIROBOT_MCP_SERVERS") or (ROOT / "mcp_servers.json")).expanduser()
INSTALL_DIR = DATA_DIR / "mcp"
requests.packages.urllib3.disable_warnings()


def load_servers() -> list[dict[str, Any]]:
    PATH.parent.mkdir(parents=True, exist_ok=True)
    if not PATH.exists():
        PATH.write_text("[]", encoding="utf-8")
    try:
        data = json.loads(PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def save_servers(servers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    PATH.write_text(json.dumps(servers, ensure_ascii=False, indent=2), encoding="utf-8")
    return load_servers()


def _slug(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", s).strip("-").lower() or "mcp-server"


def search(query: str, limit: int = 20) -> list[dict[str, Any]]:
    q = (query or "").strip()
    if not q:
        q = "mcp server"
    else:
        q = f"{q} mcp server"
    items = None
    try:
        cp = subprocess.run(
            ["gh", "api", "search/repositories", "-X", "GET",
             "-f", f"q={q}", "-f", "sort=stars", "-f", "order=desc",
             "-f", f"per_page={min(50, limit)}"],
            capture_output=True, text=True, timeout=45,
        )
        if cp.returncode == 0:
            items = json.loads(cp.stdout).get("items", [])
    except Exception:
        items = None
    if items is None:
        r = requests.get("https://api.github.com/search/repositories",
                         params={"q": q, "sort": "stars", "order": "desc", "per_page": min(50, limit)},
                         timeout=30, headers={"Accept": "application/vnd.github+json"})
        if r.status_code >= 400:
            raise RuntimeError(f"GitHub search {r.status_code}: {r.text[:300]}")
        items = r.json().get("items", [])
    out = []
    for x in items:
        out.append({
            "id": x.get("id"),
            "name": x.get("name"),
            "full_name": x.get("full_name"),
            "description": x.get("description"),
            "url": x.get("html_url"),
            "clone_url": x.get("clone_url"),
            "stars": x.get("stargazers_count"),
            "language": x.get("language"),
            "updated_at": x.get("updated_at"),
        })
    return out


def install(repo_url: str, name: str | None = None) -> dict[str, Any]:
    if not re.match(r"^(https://github\.com/|git@github\.com:)", repo_url or ""):
        raise ValueError("only GitHub repositories can be installed")
    slug = _slug(name or repo_url.rstrip("/").split("/")[-1].removesuffix(".git"))
    dst = INSTALL_DIR / slug
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        shutil.rmtree(dst)
    subprocess.run(["git", "clone", "--depth", "1", repo_url, str(dst)], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    server = {
        "id": uuid.uuid4().hex[:12],
        "name": name or slug,
        "source_url": repo_url,
        "path": str(dst),
        "transport": "stdio",
        "command": "",
        "args": [],
        "env": {},
        "enabled": False,
        "installed_at": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
    }
    servers = load_servers()
    servers.append(server)
    save_servers(servers)
    return server


def add_server(data: dict[str, Any]) -> dict[str, Any]:
    servers = load_servers()
    server = {
        "id": data.get("id") or uuid.uuid4().hex[:12],
        "name": data.get("name") or "MCP server",
        "source_url": data.get("source_url") or "",
        "path": data.get("path") or "",
        "transport": data.get("transport") or "stdio",
        "command": data.get("command") or "",
        "args": data.get("args") or [],
        "env": data.get("env") or {},
        "enabled": bool(data.get("enabled", False)),
    }
    existing = next((s for s in servers if s.get("id") == server["id"]), None)
    if existing:
        existing.update(server)
    else:
        servers.append(server)
    save_servers(servers)
    return server


def remove_server(server_id: str, delete_files: bool = False) -> None:
    servers = load_servers()
    keep = []
    for s in servers:
        if s.get("id") == server_id:
            if delete_files and s.get("path"):
                shutil.rmtree(s["path"], ignore_errors=True)
        else:
            keep.append(s)
    save_servers(keep)
