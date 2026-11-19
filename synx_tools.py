from __future__ import annotations

import json
import re
import subprocess
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from PIL import Image

import db
from config import DATA_DIR, ROOT, load_config
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
    refs_dir = DATA_DIR / "refs" / "versions" / str(info.get("project") or "project")
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


def _project_from_table(project: str) -> tuple[str, str] | None:
    text = str(project or "").strip()
    if not text:
        return None
    row = db.one(
        """SELECT project_id, project_alias FROM publications
           WHERE lower(project_alias)=lower(?) OR project_id=? LIMIT 1""",
        (text, text))
    if row:
        return str(row["project_id"]), str(row["project_alias"] or text)
    row = db.one(
        """SELECT project_id, project_alias, count(*) AS c FROM publications
           WHERE lower(project_alias) LIKE ?
           GROUP BY project_id, project_alias ORDER BY c DESC LIMIT 1""",
        (f"%{text.lower()}%",))
    if row:
        return str(row["project_id"]), str(row["project_alias"] or text)
    return None


def project_news(project: str, date_from: str | None = None, date_to: str | None = None,
                 session_id: str = "default", mark_seen: bool = True,
                 limit: int = 3000) -> dict[str, Any]:
    """Summarise messages already collected in the local scan table."""
    d1 = _date(date_to, date.today()) or date.today()
    d0 = _date(date_from, d1 - timedelta(days=6)) or (d1 - timedelta(days=6))
    found = _project_from_table(project)
    if not found:
        c = client()
        p = _project(c, project)
        found = (str(p["id"]), str(p.get("alias") or project))
    project_id, project_alias = found
    rows = db.project_publications(project_id, d0.isoformat(), d1.isoformat(), limit)
    view = db.get_project_view(session_id, project_id) or {}
    last_seen_id = int(view.get("last_seen_id") or 0)
    first_view = last_seen_id <= 0
    new_rows = [r for r in rows if int(r.get("id") or 0) > last_seen_id]

    def answered(r):
        return bool(r.get("answered_by_version"))

    def actionable(r):
        return (r.get("needs_action") == 1 and not answered(r)
                and r.get("publication_type") != "version")

    continuations = [r for r in rows if actionable(r)]
    mentions = [r for r in rows if r.get("is_mention") and not answered(r)]
    answered_rows = [r for r in rows if answered(r)]
    versions = [r for r in rows if r.get("publication_type") == "version"]
    unknown = [r for r in rows if r.get("needs_action") is None]

    groups: dict[str, dict[str, Any]] = {}
    for r in rows:
        shot = str(r.get("shot") or "—")
        g = groups.setdefault(shot, {"shot": shot, "total": 0, "continuation": 0,
                                     "mentions": 0, "versions": 0, "answered": 0,
                                     "last_at": ""})
        g["total"] += 1
        if actionable(r):
            g["continuation"] += 1
        if r.get("is_mention") and not answered(r):
            g["mentions"] += 1
        if r.get("publication_type") == "version":
            g["versions"] += 1
        if answered(r):
            g["answered"] += 1
        if str(r.get("created_at") or "") > g["last_at"]:
            g["last_at"] = str(r.get("created_at") or "")
    top_shots = sorted(groups.values(), key=lambda g: (g["continuation"] + g["mentions"],
                                                        g["total"], g["last_at"]), reverse=True)[:20]

    def brief(r: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": r.get("id"), "shot": r.get("shot"), "author": r.get("author"),
            "created_at": r.get("created_at"), "thread_kind": r.get("thread_kind"),
            "version": r.get("version_number"),
            "type": ("упоминание @p.robot" if r.get("is_mention") else
                     ("отвечено" if answered(r) else ("продолжение" if actionable(r) else "версия"))),
            "body": (str(r.get("body") or "").replace("\n", " ")[:320]),
        }

    actions = sorted(continuations, key=lambda r: (bool(r.get("is_mention")),
                                                   str(r.get("created_at") or "")), reverse=True)[:40]
    mention_items = sorted(mentions, key=lambda r: str(r.get("created_at") or ""), reverse=True)[:20]
    latest_versions = sorted([r for r in versions if r.get("version_number") is not None],
                             key=lambda r: str(r.get("created_at") or ""), reverse=True)[:20]
    new_items = sorted(new_rows, key=lambda r: (str(r.get("created_at") or ""), int(r.get("id") or 0)),
                       reverse=True)[:30]

    lines = [
        f"Проект: {project_alias}",
        f"Период: {d0.isoformat()} .. {d1.isoformat()}",
        f"Всего сообщений в таблице: {len(rows)}",
        f"Новых с прошлого просмотра: {len(new_rows)}" + (" (первый просмотр)" if first_view else ""),
        f"Продолжения/требуют действий: {len(continuations)}",
        f"Упоминания @p.robot: {len(mentions)}",
        f"Отвечено версией: {len(answered_rows)}",
        f"Версии/публикации: {len(versions)}",
        f"Не разобрано: {len(unknown)}",
        "", "Фронт работ по шотам:",
    ]
    for g in top_shots[:12]:
        lines.append(f"- {g['shot']}: всего {g['total']}, продолжений {g['continuation']}, "
                     f"упоминаний {g['mentions']}, версий {g['versions']}")
    lines.append("")
    lines.append("Требуют действий (последние):")
    for r in actions[:20]:
        mark = " @p.robot" if r.get("is_mention") else ""
        lines.append(f"- [{r.get('shot')}] {r.get('author')} {str(r.get('created_at') or '')[:16]}{mark}: "
                     f"{str(r.get('body') or '').replace(chr(10), ' ')[:220]}")
    if mention_items:
        lines.append("")
        lines.append("Упоминания @p.robot:")
        for r in mention_items[:12]:
            lines.append(f"- [{r.get('shot')}] {r.get('author')}: "
                         f"{str(r.get('body') or '').replace(chr(10), ' ')[:220]}")
    if latest_versions:
        lines.append("")
        lines.append("Последние версии:")
        for r in latest_versions[:12]:
            lines.append(f"- [{r.get('shot')}] v{int(r.get('version_number') or 0):03d} "
                         f"{str(r.get('created_at') or '')[:16]}: "
                         f"{str(r.get('body') or '').replace(chr(10), ' ')[:160]}")

    if mark_seen and rows:
        top_id = max(int(r.get("id") or 0) for r in rows)
        top_at = max(str(r.get("created_at") or "") for r in rows)
        db.set_project_view(session_id, project_id, top_id, top_at)

    return {
        "project": project_alias, "project_id": project_id,
        "date_from": d0.isoformat(), "date_to": d1.isoformat(),
        "total": len(rows), "new_since_last": len(new_rows), "first_view": first_view,
        "counts": {"continuation": len(continuations), "mentions": len(mentions),
                   "answered": len(answered_rows), "versions": len(versions),
                   "unknown": len(unknown)},
        "top_shots": top_shots, "actions": [brief(r) for r in actions],
        "mentions": [brief(r) for r in mention_items],
        "latest_versions": [brief(r) for r in latest_versions],
        "new_items": [brief(r) for r in new_items],
        "digest": "\n".join(lines),
    }


def find_entity(project: str, name: str, entity_kind: str | None = None) -> dict[str, Any]:
    c = client()
    p = _project(c, project)
    norm = re.sub(r"[-\s]+", "_", str(name or "").strip().lower())
    rows = c.entities(p["id"])
    for e in rows:
        ename = re.sub(r"[-\s]+", "_", str(e.get("name") or "").strip().lower())
        if ename == norm and (not entity_kind or e.get("entity_kind") == entity_kind):
            return e
    raise ValueError(f"entity not found: {name}")


def _entity_url(base: str, project_alias: str, entity: dict[str, Any]) -> str:
    from urllib.parse import quote
    kind = entity.get("entity_kind") or "asset"
    name = entity.get("name") or ""
    return f"{base.rstrip('/')}/p/{quote(str(project_alias or ''))}?tab=tasks&t={quote(kind + '-' + str(name))}"


def asset_types(project: str) -> dict[str, Any]:
    c = client()
    p = _project(c, project)
    return {"project": p.get("alias"), "asset_types": c.asset_types(p["id"])}


def task_types(project: str) -> dict[str, Any]:
    c = client()
    p = _project(c, project)
    return {"project": p.get("alias"), "task_types": c.task_types(p["id"])}


def create_entity(project: str, name: str, entity_kind: str = "asset",
                  asset_type: str | None = None, info: str | None = None,
                  display_name: str | None = None, cut_in: int | None = None,
                  cut_out: int | None = None, task_types: list[str] | None = None,
                  skill_codes: list[str] | None = None,
                  task_name: str | None = None,
                  linked_assets: list[str] | None = None) -> dict[str, Any]:
    c = client()
    p = _project(c, project)
    entity_kind = "shot" if str(entity_kind).lower() == "shot" else "asset"
    existing = None
    for e in c.entities(p["id"]):
        if str(e.get("name") or "").lower() == str(name).lower() and e.get("entity_kind") == entity_kind:
            existing = e
            break
    if existing:
        return {"ok": True, "exists": True, "entity": existing,
                "url": _entity_url(c.base, p.get("alias"), existing)}
    resolved_type = None
    available = []
    if entity_kind == "asset" and asset_type:
        for t in c.asset_types(p["id"]):
            code = str(t.get("code") or "")
            label = str(t.get("label") or "")
            available.append(f"{code} ({label})")
            if str(asset_type).lower() in (code.lower(), label.lower()):
                resolved_type = code
                break
        if not resolved_type:
            raise ValueError(f"unknown asset_type: {asset_type}. Available: {', '.join(available)}")
    payload = {
        "entity_kind": entity_kind, "name": name,
        "display_name": display_name or name, "info": info or "",
        "asset_type": resolved_type, "skill_codes": skill_codes or [],
    }
    if cut_in is not None:
        payload["cut_in"] = int(cut_in)
    if cut_out is not None:
        payload["cut_out"] = int(cut_out)
    e = c.create_entity(p["id"], payload)
    created_tasks = []
    # Create tasks explicitly: the API's task_types auto-naming produces a
    # generic "art" task, which does not match the folder/task convention.
    task_type_list = [str(t) for t in (task_types or []) if str(t).strip()]
    for tt in task_type_list:
        try:
            tz = (f"ТЗ ({tt}) — {display_name or e['name']}\n\n{info}" if info
                  else f"ТЗ ({tt}) — {display_name or e['name']}: описание не задано, "
                       f"заполнить перед стартом.")
            t = c.create_task(e["id"], task_name or e["name"], tt,
                              comment=tz, bid=3.0, status="todo")
            created_tasks.append({"id": t.get("id"), "task_name": t.get("task_name"),
                                  "task_type": t.get("task_type")})
        except Exception:
            pass
    linked = []
    if linked_assets:
        # EntityCreate ignores linked_asset_ids; links are written by PATCH.
        try:
            lr = link_assets(project, name, linked_assets)
            e = next((x for x in c.entities(p["id"]) if x["id"] == e["id"]), e)
            linked = lr.get("linked") or []
        except Exception:
            linked = []
    e["tasks"] = created_tasks
    return {"ok": True, "exists": False, "entity": e,
            "tasks": created_tasks, "linked": linked,
            "url": _entity_url(c.base, p.get("alias"), e)}


def create_task(project: str, shot: str, task_name: str, task_type: str,
                assignee: str | None = None, comment: str = "",
                status: str = "wip", bid: float = 3.0) -> dict[str, Any]:
    c = client()
    p = _project(c, project)
    e = find_entity(project, shot)
    types = c.task_types(p["id"])
    if types and task_type not in types:
        raise ValueError(f"invalid task_type for {e.get('entity_kind')}: {task_type}. Available: {', '.join(types)}")
    task = c.create_task(e["id"], task_name, task_type, comment=comment,
                         bid=float(bid), status=status)
    assigned = None
    if assignee:
        try:
            users = {u.get("short_name"): u for u in c.assignment_users(task["id"])}
            if assignee in users:
                c.assign(task["id"], [users[assignee]["id"]])
                assigned = assignee
        except Exception:
            pass
    return {"ok": True, "task": task, "entity": e, "assigned": assigned,
            "url": _entity_url(c.base, p.get("alias"), e)}


def entities(project: str, entity_kind: str | None = None,
             query: str | None = None, limit: int = 500) -> dict[str, Any]:
    """List project entities with counts (for review/cleanup before deletion)."""
    c = client()
    p = _project(c, project)
    rows = c.entities(p["id"])
    q = str(query or "").strip().lower()
    out = []
    for e in rows:
        if entity_kind and e.get("entity_kind") != entity_kind:
            continue
        if q and q not in str(e.get("name") or "").lower() \
                and q not in str(e.get("display_name") or "").lower():
            continue
        out.append({
            "id": e.get("id"), "name": e.get("name"),
            "display_name": e.get("display_name"),
            "entity_kind": e.get("entity_kind"),
            "asset_type": e.get("asset_type"),
            "status": e.get("status"),
            "created_by": e.get("created_by"),
            "versions_count": e.get("versions_count"),
            "tasks_count": e.get("tasks_count"),
            "sequence_name": e.get("sequence_name"),
        })
    return {"project": p.get("alias"), "count": len(out), "items": out[:int(limit)]}


def assign_task(task_id: str, bot_login: str) -> dict[str, Any]:
    """Assign a bot to an existing synx task by login/short_name."""
    c = client()
    users = c.assignment_users(task_id)
    user = next((u for u in users
                 if str(u.get("short_name") or "").lower() == str(bot_login).lower()
                 or str(u.get("login") or "").lower() == str(bot_login).lower()), None)
    if not user:
        names = ", ".join(str(u.get("short_name")) for u in users[:30])
        raise ValueError(f"бот {bot_login} недоступен для назначения. Доступные: {names}")
    c.assign(task_id, [user["id"]])
    return {"ok": True, "task_id": task_id, "assigned": bot_login,
            "user_id": user["id"]}


def update_entity(project: str, name: str, info: str | None = None,
                  display_name: str | None = None,
                  asset_type: str | None = None,
                  linked_asset_ids: list[str] | None = None,
                  linked_assets: list[str] | None = None) -> dict[str, Any]:
    """Update entity fields (ТЗ, display name, asset type, links)."""
    c = client()
    p = _project(c, project)
    e = find_entity(project, name)
    payload: dict[str, Any] = {}
    if info is not None:
        payload["info"] = info
    if display_name is not None:
        payload["display_name"] = display_name
    if asset_type is not None:
        resolved = None
        for t in c.asset_types(p["id"]):
            if str(asset_type).lower() in (str(t.get("code")).lower(),
                                          str(t.get("label")).lower()):
                resolved = t.get("code")
                break
        if not resolved:
            raise ValueError(f"unknown asset_type: {asset_type}")
        payload["asset_type"] = resolved
    if linked_assets:
        payload["linked_asset_ids"] = [find_entity(project, n)["id"] for n in linked_assets]
    elif linked_asset_ids is not None:
        payload["linked_asset_ids"] = list(linked_asset_ids)
    if not payload:
        raise ValueError("nothing to update")
    updated = c.update_entity(e["id"], payload)
    return {"ok": True, "entity": updated}


def link_assets(project: str, name: str, linked: list[str],
                bidirectional: bool = True,
                replace: bool = False) -> dict[str, Any]:
    """Link an entity to assets by name; the API stores one direction, so by
    default the reverse links are written too."""
    c = client()
    p = _project(c, project)
    e = find_entity(project, name)
    targets = [find_entity(project, n) for n in linked if str(n).strip()]
    ids = [t["id"] for t in targets if t["id"] != e["id"]]
    current = list(e.get("linked_asset_ids") or [])
    new_ids = ids if replace else list(dict.fromkeys(current + ids))
    updated = c.update_entity(e["id"], {"linked_asset_ids": new_ids})
    reverse = []
    if bidirectional:
        for t in targets:
            if t["id"] == e["id"]:
                continue
            tcur = list(t.get("linked_asset_ids") or [])
            if e["id"] in tcur:
                continue
            try:
                c.update_entity(t["id"], {"linked_asset_ids": tcur + [e["id"]]})
                reverse.append(t.get("name"))
            except Exception:
                pass
    fresh = next((x for x in c.entities(p["id"]) if x["id"] == e["id"]), updated)
    return {"ok": True, "entity": fresh.get("name"),
            "linked": [x.get("name") for x in (fresh.get("linked_assets") or [])],
            "linked_ids": fresh.get("linked_asset_ids") or [],
            "reverse": reverse}


def delete_entity(project: str, name: str | None = None,
                  entity_id: str | None = None, force: bool = False) -> dict[str, Any]:
    """Delete an asset/shot. Refuses non-empty entities unless force=True."""
    c = client()
    p = _project(c, project)
    if entity_id:
        e = next((x for x in c.entities(p["id"]) if x.get("id") == entity_id), None)
        if not e:
            raise ValueError(f"entity not found by id: {entity_id}")
    else:
        e = find_entity(project, name or "")
    versions = c.versions(e["id"])
    tasks_list = c.tasks(e["id"])
    if (versions or tasks_list) and not force:
        raise ValueError(
            f"у сущности {e.get('name')} есть версии ({len(versions)}) и задачи "
            f"({len(tasks_list)}). Удалять только с force=true, предварительно показав "
            f"пользователю, что будет удалено.")
    c.delete_entity(e["id"])
    return {"ok": True, "deleted": {"id": e.get("id"), "name": e.get("name"),
                                    "display_name": e.get("display_name"),
                                    "entity_kind": e.get("entity_kind"),
                                    "asset_type": e.get("asset_type")},
            "versions": len(versions), "tasks": len(tasks_list)}


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
