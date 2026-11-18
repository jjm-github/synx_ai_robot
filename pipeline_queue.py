"""Queue chat work as visible pipeline tasks.

Any production work requested from chat (generation, series, per-entity jobs)
must become a DB task in the pipeline queue, so it is visible in the Tasks tab
with status/progress — instead of running invisibly inside the chat turn.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import db
from config import load_config
from paths import render_dir, task_work_dir
from synx import SynxClient


def _client() -> SynxClient:
    cfg = load_config()
    return SynxClient(cfg["synx"]["base_url"], cfg["synx"]["login"], cfg["synx"]["password"])


def _resolve(project: str, name: str) -> tuple[dict[str, Any], dict[str, Any]]:
    c = _client()
    proj = next((p for p in c.projects()
                 if str(p.get("alias") or "").lower() == project.lower()
                 or str(p.get("name") or "").lower() == project.lower()), None)
    if not proj:
        raise ValueError(f"project not found: {project}")
    ent = next((e for e in c.entities(proj["id"])
                if str(e.get("name") or "").lower() == name.lower()), None)
    if not ent:
        raise ValueError(f"entity not found: {name}")
    return proj, ent


def _bot_for(task_id: str | None, requested: str | None) -> tuple[str, str | None]:
    from publish_flow import _pick_bot
    c = _client()
    if not task_id:
        return (requested or "a.smirnov"), None
    bot, user = _pick_bot(c, task_id, requested)
    return str(bot.get("login")), user.get("id")


def create_generation_task(project: str, entity: str, prompt: str, *,
                           negative: str = "",
                           task_name: str = "chat_generation",
                           task_type: str = "Art",
                           bot_login: str | None = None,
                           workflow_config: dict[str, Any] | None = None,
                           title: str | None = None) -> dict[str, Any]:
    """Create one queued pipeline task for an entity and return its ids."""
    cfg = load_config()
    c = _client()
    proj, ent = _resolve(project, entity)
    alias = str(proj.get("alias") or project)
    entity_name = str(ent.get("name") or entity)
    entity_kind = str(ent.get("entity_kind") or "asset")
    sequence = str(ent.get("sequence_name") or "")

    tasks = c.tasks(ent["id"])
    synx_task = next((t for t in tasks if t.get("task_name") == task_name), None)
    if not synx_task:
        types = c.task_types(proj["id"])
        ttype = task_type if (not types or task_type in types) else ("Art" if "Art" in types else types[0])
        synx_task = c.create_task(ent["id"], task_name, ttype,
                                  comment=f"Конвейер: {title or prompt[:120]}",
                                  bid=3.0, status="wip")
    synx_task_id = synx_task.get("id")
    bot, user_id = _bot_for(synx_task_id, bot_login)
    if user_id:
        try:
            c.assign(synx_task_id, [user_id])
        except Exception:
            pass

    versions = c.versions(ent["id"])
    next_ver = max([int(v.get("version_number") or 0) for v in versions] + [0]) + 1
    wconf = {"profile": "flux3_t2i", "steps": 26, "cfg": 2.0,
             "width": 1536, "height": 864}
    wconf.update(workflow_config or {})
    work = task_work_dir(cfg, alias, entity_name, sequence, task_type, bot,
                         task_name, next_ver, entity_kind=entity_kind)
    render = render_dir(cfg, alias, entity_name, sequence, next_ver, entity_kind=entity_kind)
    plan = {"needs_task": True, "summary": title or prompt[:200],
            "task_name": task_name, "task_type": task_type, "assignee": bot,
            "workflow_config": wconf}
    src = {"project_id": proj["id"], "project_alias": alias, "entity_id": ent["id"],
           "entity_kind": entity_kind, "shot": entity_name,
           "display_name": ent.get("display_name") or entity_name,
           "sequence": sequence, "target_version": next_ver,
           "width": int(wconf.get("width") or 1536),
           "height": int(wconf.get("height") or 864),
           "seed": int(wconf.get("seed") or 20261117)}
    tid = db.create_task({
        "publication_id": None, "synx_task_id": synx_task_id,
        "project_id": proj["id"], "entity_id": ent["id"], "shot": entity_name,
        "task_name": task_name, "task_type": task_type, "bot": bot,
        "status": "queued", "stage": "queued", "progress": 0,
        "prompt": prompt, "negative": negative or "",
        "plan_json": json.dumps(plan, ensure_ascii=False),
        "plan_md": "", "plan_md_path": "", "workflow_json": None,
        "source_json": json.dumps(src, ensure_ascii=False),
        "work_dir": str(work), "render_dir": str(render), "result_file": None,
        "version_id": None, "version_code": None, "synx_url": None,
        "error": None, "paused": 0, "skipped": 0, "priority": 0,
    })
    return {"task_id": tid, "synx_task_id": synx_task_id, "project": alias,
            "entity": entity_name, "task_name": task_name, "task_type": task_type,
            "bot": bot, "status": "queued", "work_dir": str(work)}


def queue_many(project: str, entities: list[str], prompt_fn,
               *, negative: str = "", task_name: str = "chat_generation",
               task_type: str = "Art", bot_login: str | None = None,
               workflow_config: dict[str, Any] | None = None,
               start: bool = True) -> dict[str, Any]:
    created = []
    errors = []
    for name in entities:
        try:
            created.append(create_generation_task(
                project, name, prompt_fn(name), negative=negative,
                task_name=task_name, task_type=task_type, bot_login=bot_login,
                workflow_config=workflow_config, title=name))
        except Exception as exc:
            errors.append({"entity": name, "error": str(exc)[:200]})
    if start and created:
        import pipeline
        pipeline.manager.start()
    return {"ok": not errors, "created": len(created), "tasks": created,
            "errors": errors, "pipeline": _pipeline_status()}


def _pipeline_status() -> dict[str, Any]:
    try:
        import pipeline
        return pipeline.manager.status()
    except Exception:
        return {}


def locations(project: str) -> list[str]:
    c = _client()
    proj = next((p for p in c.projects()
                 if str(p.get("alias") or "").lower() == project.lower()), None)
    if not proj:
        return []
    return [str(e.get("name")) for e in c.entities(proj["id"])
            if e.get("asset_type") == "Location"]
