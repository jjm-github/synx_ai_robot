from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from config import load_config
from synx import SynxClient


def client() -> SynxClient:
    cfg = load_config()
    return SynxClient(cfg["synx"]["base_url"], cfg["synx"]["login"], cfg["synx"]["password"])


def _project(c: SynxClient, ref: str) -> dict[str, Any]:
    for p in c.projects():
        if ref in (p.get("id"), p.get("alias"), p.get("name")) or str(ref).lower() in (
                str(p.get("alias", "")).lower(), str(p.get("name", "")).lower()):
            return p
    raise ValueError(f"project not found: {ref}")


def _date(s: str | None, default: date | None = None) -> date | None:
    if not s:
        return default
    s = str(s).strip()[:10]
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except Exception:
        return default


def _version_row(v: dict[str, Any], project_id: str) -> dict[str, Any]:
    media = v.get("media") or {}
    frames = int(media.get("frames") or 1)
    fps = media.get("fps") or 25
    return {
        "id": v.get("id"),
        "project_id": project_id,
        "entity": v.get("entity_name"),
        "entity_id": v.get("entity_id"),
        "entity_kind": v.get("entity_kind"),
        "version": v.get("version_code"),
        "version_number": v.get("version_number"),
        "task_type": v.get("task_type"),
        "task_name": v.get("task_name"),
        "status": v.get("status"),
        "author": v.get("author"),
        "created_at": v.get("created_at"),
        "media": media,
        "frames": frames,
        "fps": fps,
        "file_url": v.get("file_url"),
        "pretty_url": v.get("pretty_url"),
        "original_name": v.get("original_name"),
    }


def projects() -> dict[str, Any]:
    c = client()
    return {"projects": [{"id": p["id"], "alias": p.get("alias"), "name": p.get("name")}
                         for p in c.projects()]}


def versions(project: str, date_from: str | None = None, date_to: str | None = None,
             limit: int = 300, playable_only: bool = False) -> dict[str, Any]:
    c = client()
    p = _project(c, project)
    d0 = _date(date_from)
    d1 = _date(date_to)
    rows = []
    for v in c.project_versions(p["id"]):
        created = str(v.get("created_at") or "")[:10]
        if d0 and created and created < d0.isoformat():
            continue
        if d1 and created and created > d1.isoformat():
            continue
        media = v.get("media") or {}
        if playable_only and media.get("kind") not in ("video", "image"):
            continue
        rows.append(_version_row(v, p["id"]))
    rows.sort(key=lambda r: (r.get("created_at") or "", r.get("entity") or ""))
    total = len(rows)
    rows = rows[-max(1, min(int(limit), 2000)):]
    return {"project": p.get("alias"), "from": d0.isoformat() if d0 else None,
            "to": d1.isoformat() if d1 else None, "total": total, "versions": rows}


def tasks(project: str, status: str | None = None, limit: int = 300) -> dict[str, Any]:
    c = client()
    p = _project(c, project)
    rows = c.project_tasks(p["id"])
    if status:
        rows = [t for t in rows if (t.get("status") or "").lower() == status.lower()]
    out = [{
        "id": t.get("id"), "entity_id": t.get("entity_id"), "task_name": t.get("task_name"),
        "task_type": t.get("task_type"), "status": t.get("status"), "assigned": t.get("assigned"),
        "updated_at": t.get("updated_at"),
    } for t in rows[-max(1, min(int(limit), 2000)):]]
    return {"project": p.get("alias"), "count": len(rows), "tasks": out}


def make_playlist(project: str, date_from: str | None = None,
                  date_to: str | None = None, limit: int = 500,
                  playable_only: bool = True) -> dict[str, Any]:
    if not date_from and not date_to:
        d1 = date.today()
        d0 = d1 - timedelta(days=7)
        date_from, date_to = d0.isoformat(), d1.isoformat()
    data = versions(project, date_from, date_to, limit, playable_only=playable_only)
    items = []
    for v in data["versions"]:
        if not v.get("file_url"):
            continue
        name = v.get("original_name") or Path(v.get("pretty_url") or "").name or f"{v.get('entity')}_{v.get('version')}"
        url = v["file_url"]
        if url.startswith("/"):
            url = load_config()["synx"]["base_url"].rstrip("/") + url
        frames = int(v.get("frames") or 1)
        items.append({
            "version": v.get("version"),
            "file": name,
            "sf": 1,
            "ef": frames,
            "duration": frames,
            "fps": v.get("fps") or 25,
            "url": url,
            "checked": True,
            "original_name": name,
            "movie_url": url,
            "movie_attachment": {"name": name},
            "version_id": v.get("id"),
            "entity": v.get("entity") or "",
            "project_id": v.get("project_id"),
        })
    payload = {"synx_playlist": 1, "items": items}
    text = "<start synx playlist:\n" + json.dumps(payload, ensure_ascii=False, indent=2) + "\nend synx playlist>\n"
    return {"project": data["project"], "from": data["from"], "to": data["to"],
            "count": len(items), "text": text,
            "items": [{"entity": i["entity"], "version": i["version"], "file": i["file"]} for i in items]}
