from __future__ import annotations

import json
import re
import subprocess
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from PIL import Image

from config import ROOT, load_config
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
        "media_kind": media.get("kind"),
        "file_id": v.get("file_id"),
        "width": media.get("width"),
        "height": media.get("height"),
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


def _norm_version(value: object) -> int | None:
    m = re.search(r"(\d+)", str(value or ""))
    return int(m.group(1)) if m else None


def versions(project: str, date_from: str | None = None, date_to: str | None = None,
             limit: int = 300, playable_only: bool = False,
             shot: str | None = None, version: str | None = None) -> dict[str, Any]:
    c = client()
    p = _project(c, project)
    d0 = _date(date_from)
    d1 = _date(date_to)
    shot_norm = re.sub(r"[-\s]+", "_", str(shot or "").strip().lower())
    version_num = _norm_version(version) if version else None
    rows = []
    for v in c.project_versions(p["id"]):
        if shot_norm and re.sub(r"[-\s]+", "_", str(v.get("entity_name") or "").strip().lower()) != shot_norm:
            continue
        if version_num is not None and int(v.get("version_number") or 0) != version_num:
            continue
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
    if shot_norm or version_num is not None:
        rows = rows[-20:]
    return {"project": p.get("alias"), "from": d0.isoformat() if d0 else None,
            "to": d1.isoformat() if d1 else None, "total": total, "versions": rows}


def _entity_versions(c: SynxClient, project: dict[str, Any], shot: str) -> list[dict[str, Any]]:
    rows = c.project_versions(project["id"])
    norm = re.sub(r"[-\s]+", "_", str(shot or "").strip().lower())

    def ename(v):
        return re.sub(r"[-\s]+", "_", str(v.get("entity_name") or "").strip().lower())

    exact = [v for v in rows if ename(v) == norm]
    if exact:
        return exact
    candidates = {str(v.get("entity_name") or "") for v in rows
                  if norm and norm in ename(v)}
    if len(candidates) == 1:
        only = re.sub(r"[-\s]+", "_", next(iter(candidates)).lower())
        return [v for v in rows if ename(v) == only]
    avail = sorted({str(v.get("entity_name")) for v in rows if v.get("entity_name")})[:30]
    raise ValueError(f"shot not found: {shot}. Available examples: {', '.join(avail)}")


def find_version(project: str, shot: str, version: str | None = None) -> dict[str, Any]:
    """Find one exact version by project, shot and version code (v004 / 4)."""
    c = client()
    p = _project(c, project)
    requested_alias = p.get("alias")
    corrected = False
    try:
        rows = _entity_versions(c, p, shot)
    except ValueError as first_exc:
        # The user may name the wrong project; a shot name is usually unique.
        alternatives = []
        for other in c.projects():
            if other.get("id") == p.get("id"):
                continue
            try:
                other_rows = _entity_versions(c, other, shot)
            except Exception:
                continue
            if other_rows:
                alternatives.append((other, other_rows))
        if len(alternatives) == 1:
            p, rows = alternatives[0]
            corrected = True
        else:
            names = ", ".join(str(x[0].get("alias")) for x in alternatives)
            raise ValueError(str(first_exc) + (f". Found in other projects: {names}" if names else ""))
    vnum = _norm_version(version) if version else None
    if vnum is not None:
        matches = [v for v in rows
                   if int(v.get("version_number") or 0) == vnum
                   or str(v.get("version_code") or "").strip().lower() == str(version).strip().lower()]
        if not matches:
            codes = ", ".join(str(v.get("version_code")) for v in rows)
            raise ValueError(f"version not found: {shot} {version}. Available: {codes}")
        v = max(matches, key=lambda x: int(x.get("version_number") or 0))
    else:
        v = max(rows, key=lambda x: int(x.get("version_number") or 0))
    row = _version_row(v, p["id"])
    row["project"] = p.get("alias")
    row["project_name"] = p.get("name")
    row["pretty_url"] = v.get("pretty_url")
    if corrected:
        row["project_corrected"] = True
        row["requested_project"] = requested_alias
    return row


def prepare_reference(project: str, shot: str, version: str | None = None,
                      frame: str = "middle") -> dict[str, Any]:
    """Download a version and return a local reference image (video frame for videos)."""
    info = find_version(project, shot, version)
    c = client()
    media = info.get("media") or {}
    ext = Path(str(info.get("original_name") or "")).suffix.lower()
    image_exts = {".png", ".jpg", ".jpeg", ".webp", ".avif", ".bmp", ".tif", ".tiff"}
    video_exts = {".mp4", ".mov", ".webm", ".mkv", ".avi", ".m4v"}
    is_video = (media.get("kind") == "video") or (ext in video_exts)
    refs_dir = ROOT / "data" / "refs" / "versions" / str(info.get("project") or "project")
    refs_dir.mkdir(parents=True, exist_ok=True)
    base = f"{info.get('entity') or shot}_{info.get('version') or 'version'}"
    downloaded = refs_dir / (base + (ext if ext in (image_exts | video_exts) else (".mp4" if is_video else ".png")))
    if not info.get("file_id"):
        raise ValueError("version has no file_id")
    if not downloaded.is_file():
        c.download_file(info["file_id"], downloaded)
    dst = refs_dir / (base + ".png")
    if not is_video:
        with Image.open(downloaded) as im:
            im = im.convert("RGB")
            im.save(dst, "PNG")
            width, height = im.size
    else:
        frames = int(media.get("frames") or 1)
        n = 0
        if frame == "last":
            n = max(0, frames - 1)
        elif frame != "first":
            n = max(0, frames // 2)
        cmd = ["ffmpeg", "-v", "error", "-y", "-i", str(downloaded),
               "-vf", f"select=eq(n\\,{n}),setsar=1", "-frames:v", "1", str(dst)]
        cp = subprocess.run(cmd, capture_output=True, text=True)
        if cp.returncode != 0 or not dst.is_file():
            # fall back to the first frame if the requested frame could not be decoded
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(downloaded),
                            "-vf", "setsar=1", "-frames:v", "1", str(dst)], check=True)
        with Image.open(dst) as im:
            width, height = im.size
    return {"path": str(dst), "width": width, "height": height,
            "aspect_ratio": round(width / height, 6) if height else None,
            "frame": frame, "version": info}


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
