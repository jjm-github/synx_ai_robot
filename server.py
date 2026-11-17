from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, UploadFile, File
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import db
from ai import AIClient, AIError
from config import DATA_DIR, ROOT, load_bots, load_config, save_bots, save_config, save_system_prompt
from paths import render_dir, task_work_dir
from classify import classify_publication
import fs_tools
import mcp_manager
from links import task_version_url
from plan_md import save_plan_markdown
from chat import (IMAGES as CHAT_IMAGES, UPLOADS as CHAT_UPLOADS,
                  chat_turn, generate_image, get_chat_job, start_chat_job)
from pipeline import manager
from synx import SynxClient, SynxError

app = FastAPI(title="Synx AI Robot", version="0.1.0")
db.init()
app.mount("/static", StaticFiles(directory=str(ROOT / "static")), name="static")


class ConfigBody(BaseModel):
    config: dict[str, Any]


class BotsBody(BaseModel):
    bots: list[dict[str, Any]]


class PromptBody(BaseModel):
    system_prompt: str


class ScanBody(BaseModel):
    projects: list[str] | None = None


class AnalyzeBody(BaseModel):
    pass


class CreateTaskBody(BaseModel):
    plan: dict[str, Any] | None = None


class ChatSendBody(BaseModel):
    session_id: str = "default"
    text: str = ""
    images: list[dict[str, Any]] = []


class ChatSessionBody(BaseModel):
    title: str = "Новый чат"


class RootsBody(BaseModel):
    roots: list[str] = []


class RootAddBody(BaseModel):
    path: str


class MCPInstallBody(BaseModel):
    url: str
    name: str | None = None


class MCPServerBody(BaseModel):
    server: dict[str, Any]


def _synx() -> SynxClient:
    cfg = load_config()
    return SynxClient(cfg["synx"]["base_url"], cfg["synx"]["login"], cfg["synx"]["password"])


def _project_for(client: SynxClient, project_id: str | None = None, alias: str | None = None) -> dict[str, Any]:
    projects = client.projects()
    for p in projects:
        if project_id and p.get("id") == project_id:
            return p
        if alias and p.get("alias") == alias:
            return p
    raise HTTPException(404, "project not found")




def _analyze_publication(pid: int) -> dict[str, Any]:
    pub = db.get_publication(pid)
    if not pub:
        raise ValueError("publication not found")
    if pub.get("answered_by_version"):
        vnum = pub.get("answer_version_number")
        plan = {"needs_task": False, "task_type": None,
                "summary": "Комментарий уже отвечен версией" + (f" v{int(vnum):03d}" if vnum else ""),
                "reason": "answered_by_version"}
        db.update_publication(
            pid, status="no_task", analysis_json=json.dumps(plan, ensure_ascii=False),
            needs_action=0, publication_type="version", analysis_source="reply",
        )
        return plan
    cfg = load_config()
    client = _synx()
    project = _project_for(client, project_id=pub["project_id"])
    try:
        task_types = client.task_types(pub["project_id"])
    except Exception:
        task_types = []
    plan = AIClient(cfg).analyze_publication(pub, project, task_types, load_bots(), client)
    usage = plan.get("_usage") or {}
    md, md_path = save_plan_markdown(plan, pub)
    needs = 1 if plan.get("needs_task", True) else 0
    db.update_publication(
        pid,
        status="analyzed" if needs else "no_task",
        analysis_json=json.dumps(plan, ensure_ascii=False),
        analysis_md=md,
        analysis_md_path=md_path,
        needs_action=needs,
        publication_type="continuation" if needs else "version",
        analysis_source="ai",
        analysis_tokens=int(usage.get("input") or 0) + int(usage.get("output") or 0),
    )
    return plan


def _auto_analyze(ids: list[int]) -> None:
    for pid in ids:
        try:
            _analyze_publication(pid)
        except Exception as exc:
            db.update_publication(pid, status="analysis_error")
            db.add_event(None, "analysis_error", f"publication {pid}: {exc}")


def _scan_once(projects: list[str] | None = None) -> dict[str, Any]:
    cfg = load_config()
    aliases = projects or cfg["synx"].get("projects") or []
    client = _synx()
    total = inserted = 0
    detail = []
    new_ids: list[int] = []
    for alias in aliases:
        recs = client.scan_project(
            alias,
            include_task_comments=bool(cfg["synx"].get("include_task_comments", True)),
            workers=int(cfg["synx"].get("scan_workers", 12)),
        )
        total += len(recs)
        n = 0
        for rec in recs:
            new_id = db.add_publication(rec)
            if new_id:
                n += 1
                new_ids.append(new_id)
            else:
                db.sync_publication_mention(rec.get("comment_id"), bool(rec.get("is_mention")))
                db.sync_publication_answer(
                    rec.get("comment_id"), bool(rec.get("answered_by_version")),
                    rec.get("answer_version_number"), rec.get("answer_version_code"),
                )
                row = db.one("SELECT id, analysis_source FROM publications WHERE comment_id=?",
                             (rec.get("comment_id"),))
                if (rec.get("is_mention") and row and row.get("analysis_source") != "ai"
                        and int(row["id"]) not in new_ids):
                    new_ids.append(int(row["id"]))
        inserted += n
        detail.append({"project": alias, "comments": len(recs), "new": n})
    event_recs = client.scan_mention_events(limit=int(cfg["synx"].get("mention_events_limit", 1000)))
    event_new = 0
    for rec in event_recs:
        new_id = db.add_publication(rec)
        if new_id:
            event_new += 1
            new_ids.append(new_id)
        else:
            db.sync_publication_mention(rec.get("comment_id"), bool(rec.get("is_mention")))
            db.sync_publication_answer(
                rec.get("comment_id"), bool(rec.get("answered_by_version")),
                rec.get("answer_version_number"), rec.get("answer_version_code"),
            )
            row = db.one("SELECT id, analysis_source FROM publications WHERE comment_id=?",
                         (rec.get("comment_id"),))
            if (rec.get("is_mention") and row and row.get("analysis_source") != "ai"
                    and int(row["id"]) not in new_ids):
                new_ids.append(int(row["id"]))
    inserted += event_new
    detail.append({"project": "@p.robot events", "comments": len(event_recs), "new": event_new})
    db.set_meta("last_scan", db.now())
    ready = AIClient(cfg).ready
    if new_ids and cfg["synx"].get("auto_analyze", True) and ready:
        threading.Thread(target=_auto_analyze, args=(new_ids,), daemon=True,
                         name="synx-autoclassify").start()
    return {"ok": True, "comments": total, "new": inserted, "events": len(event_recs),
            "detail": detail,
            "auto_analyze": len(new_ids) if (cfg["synx"].get("auto_analyze", True) and ready) else 0}


def _scanner_loop() -> None:
    while True:
        cfg = load_config()
        interval = int(cfg["synx"].get("scan_interval_sec") or 0)
        if interval > 0 and cfg["synx"].get("auto_scan", True):
            try:
                _scan_once()
            except Exception as exc:
                db.set_meta("last_scan_error", str(exc)[:1000])
        time.sleep(max(30, interval or 300))


_scanner_thread: threading.Thread | None = None


@app.on_event("startup")
def _startup():
    global _scanner_thread
    if _scanner_thread is None or not _scanner_thread.is_alive():
        _scanner_thread = threading.Thread(target=_scanner_loop, name="synx-scanner", daemon=True)
        _scanner_thread.start()


@app.get("/")
def index():
    return FileResponse(ROOT / "static" / "index.html")


@app.get("/api/health")
def health():
    return {"ok": True, "db": str(db.DB_PATH), "pipeline": manager.status()}


@app.get("/api/config")
def get_config():
    return load_config()


@app.put("/api/config")
def put_config(body: ConfigBody):
    cfg = body.config
    prompt = cfg.get("ai", {}).pop("system_prompt", None)
    if prompt is not None:
        save_system_prompt(prompt)
    save_config(cfg)
    return load_config()


@app.get("/api/bots")
def get_bots():
    return {"bots": load_bots()}


@app.put("/api/bots")
def put_bots(body: BotsBody):
    return {"bots": save_bots(body.bots)}


@app.get("/api/system-prompt")
def get_prompt():
    return {"system_prompt": load_config()["ai"]["system_prompt"]}


@app.put("/api/system-prompt")
def put_prompt(body: PromptBody):
    save_system_prompt(body.system_prompt)
    return {"ok": True}


@app.post("/api/ai/test")
def ai_test():
    try:
        return AIClient().test()
    except Exception as exc:
        raise HTTPException(400, str(exc))


@app.get("/api/synx/projects")
def synx_projects():
    try:
        return {"projects": _synx().projects()}
    except Exception as exc:
        raise HTTPException(400, str(exc))


@app.get("/api/synx/task-types")
def synx_task_types(project_id: str):
    try:
        return {"task_types": _synx().task_types(project_id)}
    except Exception as exc:
        raise HTTPException(400, str(exc))


@app.post("/api/synx/scan")
def synx_scan(body: ScanBody):
    try:
        return _scan_once(body.projects)
    except Exception as exc:
        raise HTTPException(400, str(exc))


@app.get("/api/publications")
def publications(status: str | None = None, limit: int = 5000):
    return {"publications": db.list_publications(status, limit)}


@app.post("/api/publications/{pid}/analyze")
def analyze_publication(pid: int):
    try:
        plan = _analyze_publication(pid)
    except Exception as exc:
        db.update_publication(pid, status="analysis_error")
        raise HTTPException(400, str(exc))
    return {"ok": True, "plan": plan}


@app.post("/api/publications/reclassify")
def reclassify_publications(ai: bool = True):
    rows = db.list_publications(limit=5000)
    counts = {"version": 0, "continuation": 0, "unknown": 0, "needs_ai": 0}
    need_ai: list[int] = []
    for pub in rows:
        c = classify_publication(pub)
        db.update_publication(
            pub["id"],
            needs_action=c["needs_action"],
            publication_type=c["publication_type"],
            analysis_source=c["source"],
        )
        key = c["publication_type"] if c["publication_type"] in counts else "unknown"
        counts[key] = counts.get(key, 0) + 1
        if c["source"] == "needs_ai":
            need_ai.append(int(pub["id"]))
    counts["needs_ai"] = len(need_ai)
    if ai and need_ai and AIClient().ready:
        threading.Thread(target=_auto_analyze, args=(need_ai,), daemon=True,
                         name="synx-reclassify-ai").start()
    elif ai and need_ai and not AIClient().ready:
        counts["needs_ai"] = len(need_ai)
    return {"ok": True, "counts": counts, "ai_queued": len(need_ai) if ai and AIClient().ready else 0}


@app.post("/api/publications/analyze-batch")
def analyze_batch(limit: int = 100):
    rows = db.all("SELECT id FROM publications WHERE analysis_json IS NULL ORDER BY id ASC LIMIT ?", (limit,))
    ids = [int(r["id"]) for r in rows]
    if ids and not AIClient().ready:
        raise HTTPException(400, "AI provider is not configured")
    if ids:
        threading.Thread(target=_auto_analyze, args=(ids,), daemon=True,
                         name="synx-autoclassify").start()
    return {"ok": True, "queued": len(ids)}


@app.post("/api/publications/{pid}/create-task")
def create_task_from_publication(pid: int, body: CreateTaskBody | None = None):
    pub = db.get_publication(pid)
    if not pub:
        raise HTTPException(404, "publication not found")
    plan = dict((body.plan if body and body.plan else pub.get("analysis")) or {})
    if pub.get("is_mention"):
        guidance = re.sub(r"@p\.robot\b", "", pub.get("body") or "", flags=re.I).strip()
        plan["needs_task"] = True
        plan["priority"] = "high"
        plan["addressed_to_robot"] = True
        if not plan.get("summary"):
            plan["summary"] = guidance or "Приоритетное указание для @p.robot"
        if not plan.get("prompt"):
            plan["prompt"] = guidance or plan["summary"]
    if pub.get("answered_by_version"):
        vnum = pub.get("answer_version_number")
        raise HTTPException(
            400,
            "по этому комментарию уже выпущена версия"
            + (f" v{int(vnum):03d}" if vnum else "")
            + " — продолжение не требуется",
        )
    if not plan.get("needs_task", True):
        raise HTTPException(400, "AI says no task is needed")
    client = _synx()
    project = _project_for(client, project_id=pub["project_id"])
    task_types = client.task_types(project["id"])
    task_type = plan.get("task_type") or "prod_gen"
    if isinstance(task_type, dict):
        task_type = task_type.get("code") or task_type.get("name") or task_type.get("id") or "prod_gen"
    task_type = str(task_type)
    if task_types and task_type not in task_types:
        task_type = "prod_gen" if "prod_gen" in task_types else task_types[0]
    task_name = re.sub(r"[^a-zA-Z0-9_]+", "_", str(plan.get("task_name") or "robot_task")).strip("_")[:48] or "robot_task"
    comment = plan.get("summary") or pub.get("body") or "Synx AI Robot task"
    try:
        synx_task = client.create_task(pub["entity_id"], task_name, task_type,
                                       comment=comment, bid=3.0, status="wip")
    except SynxError as exc:
        # 409: reuse an existing task with the same name on the entity
        if "409" not in str(exc):
            raise HTTPException(400, str(exc))
        existing = next((t for t in client.tasks(pub["entity_id"]) if t.get("task_name") == task_name), None)
        if not existing:
            raise HTTPException(400, str(exc))
        synx_task = existing
    bot_login = plan.get("assignee") or "e.matveev"
    bots = {b["login"]: b for b in load_bots() if b.get("enabled", True)}
    if bot_login not in bots:
        bot_login = next(iter(bots), "e.matveev")
    try:
        users = {u["short_name"]: u["id"] for u in client.assignment_users(synx_task["id"])}
    except Exception:
        users = {b["login"]: b["user_id"] for b in load_bots() if b.get("user_id")}
    if bot_login in users:
        client.assign(synx_task["id"], [users[bot_login]])
    cfg = load_config()
    entity = next((e for e in client.entities(pub["project_id"]) if e.get("id") == pub["entity_id"]), {})
    sequence = entity.get("sequence_name") or ""
    versions = client.versions(pub["entity_id"])
    source_version = next((v for v in versions if v.get("id") == pub.get("version_id")), None) or (versions[-1] if versions else {})
    next_ver = max([int(v.get("version_number") or 0) for v in versions] + [0]) + 1
    media = source_version.get("media") or {}
    target_w, target_h = 2048, 864
    frames = int(media.get("frames") or 144)
    work = task_work_dir(cfg, project.get("alias", "project"), pub.get("shot"), sequence,
                         task_type, bot_login, task_name, next_ver)
    work.mkdir(parents=True, exist_ok=True)
    renders = render_dir(cfg, project.get("alias", "project"), pub.get("shot"), sequence, next_ver)
    renders.mkdir(parents=True, exist_ok=True)
    plan_for_task = {k: v for k, v in plan.items() if not k.startswith("_")}
    plan_for_task["_usage"] = plan.get("_usage")
    source = {
        "project_id": pub["project_id"], "project_alias": project.get("alias"),
        "entity_id": pub["entity_id"], "shot": pub.get("shot"), "sequence": sequence,
        "entity_kind": entity.get("entity_kind") or "shot",
        "entity_name": entity.get("name") or pub.get("shot"),
        "version_id": source_version.get("id"), "version_code": source_version.get("version_code"),
        "version_number": pub.get("version_number"), "comment_id": pub.get("comment_id"),
        "references": plan.get("_references_saved") or [], "media": media,
        "target_version": next_ver, "width": target_w, "height": target_h,
        "frames": frames, "seed": 20261117,
    }
    task_md = pub.get("analysis_md") or ""
    task_md_path = str(work / "PLAN.md")
    if task_md:
        (work / "PLAN.md").write_text(task_md, encoding="utf-8")
    tid = db.create_task({
        "publication_id": pid,
        "synx_task_id": synx_task["id"],
        "project_id": pub["project_id"],
        "entity_id": pub["entity_id"],
        "shot": pub.get("shot"),
        "task_name": task_name,
        "task_type": task_type,
        "bot": bot_login,
        "status": "queued",
        "stage": "queued",
        "progress": 0,
        "prompt": plan.get("prompt") or "",
        "negative": plan.get("negative") or "",
        "plan_json": json.dumps(plan_for_task, ensure_ascii=False),
        "plan_md": task_md,
        "plan_md_path": task_md_path,
        "workflow_json": json.dumps(plan.get("workflow"), ensure_ascii=False) if plan.get("workflow") else None,
        "source_json": json.dumps(source, ensure_ascii=False),
        "work_dir": str(work),
        "render_dir": str(renders),
        "priority": 1 if pub.get("is_mention") and not pub.get("answered_by_version") else 0,
    })
    db.update_publication(pid, status="task_created")
    return {"ok": True, "task": db.get_task(tid), "synx_task": synx_task}


@app.get("/api/mcp/servers")
def mcp_servers():
    return {"servers": mcp_manager.load_servers()}


@app.post("/api/mcp/servers")
def mcp_server_add(body: MCPServerBody):
    return {"server": mcp_manager.add_server(body.server)}


@app.delete("/api/mcp/servers/{server_id}")
def mcp_server_remove(server_id: str, delete_files: bool = False):
    mcp_manager.remove_server(server_id, delete_files)
    return {"ok": True}


@app.post("/api/mcp/search")
def mcp_search(body: dict[str, Any]):
    try:
        return {"results": mcp_manager.search(body.get("query") or "", int(body.get("limit") or 20))}
    except Exception as exc:
        raise HTTPException(400, str(exc))


@app.post("/api/mcp/install")
def mcp_install(body: MCPInstallBody):
    try:
        return {"server": mcp_manager.install(body.url, body.name)}
    except Exception as exc:
        raise HTTPException(400, str(exc))


@app.get("/api/filesystem/roots")
def fs_roots():
    return {"roots": fs_tools.roots()}


@app.put("/api/filesystem/roots")
def fs_roots_set(body: RootsBody):
    cfg = load_config()
    cfg.setdefault("filesystem", {})["roots"] = body.roots
    save_config(cfg)
    return {"roots": fs_tools.roots()}


@app.post("/api/filesystem/roots")
def fs_root_add(body: RootAddBody):
    try:
        return {"roots": fs_tools.add_root(body.path)}
    except Exception as exc:
        raise HTTPException(400, str(exc))


@app.delete("/api/filesystem/roots")
def fs_root_remove(path: str):
    return {"roots": fs_tools.remove_root(path)}


@app.get("/api/fs/file")
def fs_file(path: str):
    try:
        p = fs_tools.resolve(path)
        if not p.is_file():
            raise FileNotFoundError(path)
        return FileResponse(p)
    except Exception as exc:
        raise HTTPException(404, str(exc))


@app.get("/api/chat/sessions")
def chat_sessions():
    return {"sessions": db.list_chat_sessions()}


@app.post("/api/chat/sessions")
def chat_session_create(body: ChatSessionBody):
    return {"session": db.create_chat_session(body.title or "Новый чат")}


@app.delete("/api/chat/sessions/{session_id}")
def chat_session_delete(session_id: str):
    db.delete_chat_session(session_id)
    return {"ok": True}


@app.get("/api/chat/messages")
def chat_messages(session_id: str = "default"):
    return {"messages": db.list_chat_messages(session_id)}


@app.post("/api/chat/send")
def chat_send(body: ChatSendBody):
    if not (body.text or "").strip() and not body.images:
        raise HTTPException(400, "empty message")
    try:
        jid = start_chat_job(body.session_id or "default", body.text or "", body.images)
        return {"ok": True, "job_id": jid}
    except Exception as exc:
        raise HTTPException(400, str(exc))


@app.get("/api/chat/jobs/{job_id}")
def chat_job(job_id: str):
    job = get_chat_job(job_id)
    if not job:
        raise HTTPException(404, "job not found")
    return job


@app.post("/api/chat/upload")
async def chat_upload(file: UploadFile = File(...)):
    import uuid
    from PIL import Image
    raw = await file.read()
    if not raw:
        raise HTTPException(400, "empty file")
    tmp = CHAT_UPLOADS / f"{uuid.uuid4().hex[:12]}.upload"
    tmp.write_bytes(raw)
    try:
        im = Image.open(tmp).convert("RGB")
    except Exception as exc:
        tmp.unlink(missing_ok=True)
        raise HTTPException(400, f"not an image: {exc}")
    name = f"{uuid.uuid4().hex[:12]}.png"
    dst = CHAT_UPLOADS / name
    im.save(dst)
    tmp.unlink(missing_ok=True)
    return {"ok": True, "name": file.filename or name, "path": str(dst),
            "url": f"/api/chat/file/uploads/{name}"}


@app.post("/api/chat/clear")
def chat_clear(session_id: str = "default"):
    db.clear_chat(session_id)
    return {"ok": True}


@app.get("/api/chat/file/{kind}/{name}")
def chat_file(kind: str, name: str):
    base = CHAT_UPLOADS if kind == "uploads" else CHAT_IMAGES
    path = (base / Path(name).name)
    if not path.is_file():
        raise HTTPException(404, "file not found")
    return FileResponse(path)


@app.get("/api/chat/generate")
def chat_generate_info():
    return {"ok": True, "hint": "POST /api/chat/send with text; or use chat tool calling"}


@app.get("/api/tasks")
def tasks(limit: int = 500):
    return {"tasks": db.list_tasks(limit)}


@app.get("/api/tasks/active")
def active_task():
    row = db.active_task()
    if row:
        # elapsed_sec is refreshed by the pipeline heartbeat; add a live estimate
        return {"task": row}
    return {"task": None}


@app.post("/api/tasks/{tid}/pause")
def pause_task(tid: int):
    db.update_task(tid, paused=1, status="paused", stage="paused")
    db.add_event(tid, "pause", "task paused")
    return {"ok": True}


@app.post("/api/tasks/{tid}/resume")
def resume_task(tid: int):
    db.update_task(tid, paused=0, status="queued", stage="queued")
    db.add_event(tid, "resume", "task resumed")
    return {"ok": True}


@app.post("/api/tasks/{tid}/skip")
def skip_task(tid: int):
    db.update_task(tid, skipped=1, status="skipped", stage="skipped")
    db.add_event(tid, "skip", "task skipped")
    return {"ok": True}


@app.post("/api/tasks/{tid}/unskip")
def unskip_task(tid: int):
    db.update_task(tid, skipped=0, status="queued", stage="queued")
    db.add_event(tid, "unskip", "task unskipped")
    return {"ok": True}


@app.delete("/api/tasks/{tid}")
def delete_task(tid: int, delete_files: bool = Query(False)):
    task = db.get_task(tid)
    if not task:
        raise HTTPException(404, "task not found")
    if delete_files and task.get("work_dir"):
        shutil.rmtree(task["work_dir"], ignore_errors=True)
    db.delete_task(tid)
    return {"ok": True}


@app.post("/api/tasks/delete-done")
def delete_done():
    n = db.delete_done()
    return {"ok": True, "deleted": n}


@app.post("/api/tasks/{tid}/open-folder")
def open_folder(tid: int):
    task = db.get_task(tid)
    if not task:
        raise HTTPException(404, "task not found")
    path = Path(task.get("work_dir") or "")
    path.mkdir(parents=True, exist_ok=True)
    try:
        if platform.system() == "Windows":
            os.startfile(str(path))  # type: ignore[attr-defined]
        elif platform.system() == "Darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])
    except Exception as exc:
        raise HTTPException(400, f"cannot open folder: {exc}")
    return {"ok": True, "path": str(path)}


@app.post("/api/tasks/{tid}/publish")
def publish_task(tid: int):
    task = db.get_task(tid)
    if not task:
        raise HTTPException(404, "task not found")
    if not task.get("result_file"):
        raise HTTPException(400, "task has no result file")
    cfg = load_config()
    bot = next((b for b in load_bots() if b.get("login") == task.get("bot") and b.get("enabled", True)), None)
    if not bot:
        raise HTTPException(400, f"bot not found: {task.get('bot')}")
    client = SynxClient(cfg["synx"]["base_url"], bot["login"], bot["password"])
    try:
        v = client.publish_version(task["entity_id"], task["result_file"], task.get("synx_task_id") or "",
                                   comment=(task.get("plan_json") or {}).get("publish_comment") or "robot version",
                                   minutes=10, note="Synx AI Robot")
    except Exception as exc:
        raise HTTPException(400, str(exc))
    t2 = dict(task)
    t2["version_code"] = v.get("version_code")
    url = task_version_url(cfg, t2)
    db.update_task(tid, version_id=v.get("id"), version_code=v.get("version_code"),
                   synx_url=url, status="done", stage="done", progress=100,
                   finished_at=db.now())
    return {"ok": True, "version": v, "url": url}


@app.post("/api/pipeline/start")
def pipeline_start():
    return manager.start()


@app.post("/api/pipeline/stop")
def pipeline_stop():
    return manager.stop()


@app.get("/api/pipeline/status")
def pipeline_status():
    return manager.status()


@app.get("/api/log")
def log(limit: int = 200):
    return {"events": db.all("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))}


@app.get("/api/meta")
def meta():
    return {"last_scan": db.get_meta("last_scan"), "db": str(db.DB_PATH), "data": str(DATA_DIR)}
