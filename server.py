from __future__ import annotations

import json
import mimetypes
import os
import platform
import re
import shutil
import subprocess
import threading
import time
import uuid

import requests
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, UploadFile, File
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import db
from ai import AIClient, AIError
from config import DATA_DIR, ROOT, load_bots, load_config, save_bots, save_config, save_system_prompt
import fs_tools
import mcp_manager
import pdf_cache
import voice
from links import task_version_url
from plan_md import save_plan_markdown
from chat import (IMAGES as CHAT_IMAGES, UPLOADS as CHAT_UPLOADS,
                  chat_turn, chat_export_markdown, generate_image,
                  get_chat_job, start_chat_job, stop_chat_job)
from pipeline import manager
from synx import SynxClient

app = FastAPI(title="iiRobot", version="0.1.0")


@app.middleware("http")
async def _no_cache_static(request, call_next):
    response = await call_next(request)
    path = request.url.path
    if path == "/" or path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
    return response
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


class PdfRenderBody(BaseModel):
    path: str
    dpi: int = 200
    workers: int = 4


class ChatContextBody(BaseModel):
    session_id: str = "default"
    markdown: str = ""


class ChatSendBody(BaseModel):
    session_id: str = "default"
    text: str = ""
    images: list[dict[str, Any]] = []


class VoiceToggleBody(BaseModel):
    enabled: bool = True


class VoiceModelBody(BaseModel):
    model: str | None = None


class UiZoomBody(BaseModel):
    zoom: float = 1.0


class UiThemeBody(BaseModel):
    id: str = ""
    colors: dict[str, str] = {}


class ChatPanelsBody(BaseModel):
    composer_height: float | None = None
    sidebar_collapsed: bool | None = None


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
                db.update_publication_scan(rec)
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


def _event_monitor_once() -> dict[str, Any]:
    cfg = load_config()
    client = _synx()
    recs = client.scan_events(limit=int(cfg["synx"].get("event_limit", 200)),
                              only_mentions=False)
    new_ids: list[int] = []
    for rec in recs:
        new_id = db.add_publication(rec)
        if new_id:
            new_ids.append(new_id)
        else:
            db.sync_publication_mention(rec.get("comment_id"), bool(rec.get("is_mention")))
            db.sync_publication_answer(
                rec.get("comment_id"), bool(rec.get("answered_by_version")),
                rec.get("answer_version_number"), rec.get("answer_version_code"),
            )
    db.set_meta("last_event_sync", db.now())
    ready = AIClient(cfg).ready
    if new_ids and cfg["synx"].get("auto_analyze", True) and ready:
        threading.Thread(target=_auto_analyze, args=(new_ids,), daemon=True,
                         name="synx-event-autoclassify").start()
    return {"events": len(recs), "new": len(new_ids)}


def _event_monitor_loop() -> None:
    while True:
        cfg = load_config()
        interval = int(cfg["synx"].get("event_interval_sec") or 0)
        if interval > 0 and cfg["synx"].get("auto_scan", True):
            try:
                _event_monitor_once()
            except Exception as exc:
                db.set_meta("last_event_sync_error", str(exc)[:1000])
        time.sleep(max(10, interval or 30))


_scanner_thread: threading.Thread | None = None
_event_thread: threading.Thread | None = None


@app.get("/api/ui/zoom")
def ui_zoom_get():
    try:
        data = json.loads((DATA_DIR / "ui_zoom.json").read_text(encoding="utf-8"))
        return {"zoom": float(data.get("zoom", 1.0))}
    except Exception:
        return {"zoom": 1.0}


@app.post("/api/ui/zoom")
def ui_zoom_set(body: UiZoomBody):
    zoom = max(0.7, min(1.6, float(body.zoom)))
    (DATA_DIR / "ui_zoom.json").write_text(json.dumps({"zoom": zoom}), encoding="utf-8")
    return {"zoom": zoom}


@app.get("/api/ui/theme")
def ui_theme_get():
    try:
        return json.loads((DATA_DIR / "ui_theme.json").read_text(encoding="utf-8"))
    except Exception:
        return {"id": "", "colors": {}}


@app.post("/api/ui/theme")
def ui_theme_set(body: UiThemeBody):
    colors = {}
    for key in ("bg", "field", "text", "sel"):
        value = str((body.colors or {}).get(key) or "").strip()
        if re.fullmatch(r"#[0-9a-fA-F]{6}", value):
            colors[key] = value
    data = {"id": str(body.id or "")[:80], "colors": colors}
    (DATA_DIR / "ui_theme.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return data


@app.get("/api/ui/chat-panels")
def chat_panels_get():
    try:
        data = json.loads((DATA_DIR / "ui_chat_panels.json").read_text(encoding="utf-8"))
    except Exception:
        data = {}
    return {"composer_height": float(data.get("composer_height") or 0),
            "sidebar_collapsed": bool(data.get("sidebar_collapsed"))}


@app.post("/api/ui/chat-panels")
def chat_panels_set(body: ChatPanelsBody):
    path = DATA_DIR / "ui_chat_panels.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        data = {}
    if body.composer_height is not None:
        data["composer_height"] = max(0.0, min(2000.0, float(body.composer_height)))
    if body.sidebar_collapsed is not None:
        data["sidebar_collapsed"] = bool(body.sidebar_collapsed)
    data.setdefault("composer_height", 0)
    data.setdefault("sidebar_collapsed", False)
    path.write_text(json.dumps(data), encoding="utf-8")
    return data


@app.on_event("startup")
def _startup():
    global _scanner_thread, _event_thread
    if _scanner_thread is None or not _scanner_thread.is_alive():
        _scanner_thread = threading.Thread(target=_scanner_loop, name="synx-scanner", daemon=True)
        _scanner_thread.start()
    if _event_thread is None or not _event_thread.is_alive():
        _event_thread = threading.Thread(target=_event_monitor_loop, name="synx-events", daemon=True)
        _event_thread.start()


@app.get("/")
def index():
    html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    state: dict[str, Any] = {}
    try:
        theme = json.loads((DATA_DIR / "ui_theme.json").read_text(encoding="utf-8"))
    except Exception:
        theme = {}
    theme_id = str(theme.get("id") or "").strip()
    if theme_id:
        state["theme"] = theme_id
    colors = theme.get("colors") or {}
    keys = ("bg", "field", "text", "sel")
    if all(re.fullmatch(r"#[0-9a-fA-F]{6}", str(colors.get(k) or "")) for k in keys):
        state["theme_colors"] = ",".join(str(colors[k]) for k in keys)
    try:
        panels = json.loads((DATA_DIR / "ui_chat_panels.json").read_text(encoding="utf-8"))
        if panels.get("sidebar_collapsed"):
            state["sidebar_collapsed"] = True
    except Exception:
        pass
    inject = ("<script>window.SYNX_UI_STATE = "
              + json.dumps(state, ensure_ascii=False) + ";</script>\n  ")
    html = html.replace('<script src="/static/theme.js"></script>',
                        inject + '<script src="/static/theme.js"></script>', 1)
    return HTMLResponse(html, headers={
        "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"})


@app.get("/api/health")
def health():
    return {"ok": True, "db": str(db.DB_PATH), "pipeline": manager.status()}


@app.get("/api/config")
def get_config():
    return load_config()


@app.put("/api/config")
def put_config(body: ConfigBody):
    before = load_config()
    cfg = body.config
    prompt = cfg.get("ai", {}).pop("system_prompt", None)
    if prompt is not None:
        save_system_prompt(prompt)
    save_config(cfg)
    old = (before.get("voice") or {}).get("correct") or {}
    new = (cfg.get("voice") or {}).get("correct") or {}
    old_model = str(old.get("model") or "")
    if old_model and (old_model != str(new.get("model") or "")
                      or (str(old.get("provider") or "") == "ollama"
                          and str(new.get("provider") or "") != "ollama")):
        try:
            voice.engine.unload_model(old_model)
        except Exception:
            pass
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


@app.post("/api/pdf/render")
def pdf_render(body: PdfRenderBody):
    try:
        return pdf_cache.start(body.path, dpi=body.dpi, workers=body.workers)
    except Exception as exc:
        raise HTTPException(400, str(exc))


@app.get("/api/pdf/render/{job_id}")
def pdf_render_status(job_id: str):
    try:
        return pdf_cache.status(job_id)
    except KeyError:
        raise HTTPException(404, "render job not found")


@app.get("/api/pdf/page/{job_id}/{page}")
def pdf_page(job_id: str, page: int):
    try:
        return FileResponse(pdf_cache.page_file(job_id, page))
    except KeyError:
        raise HTTPException(404, "render job not found")
    except FileNotFoundError:
        raise HTTPException(404, "page not ready")


@app.get("/api/chat/export")
def chat_export(session_id: str = "default"):
    return chat_export_markdown(session_id)


@app.post("/api/chat/context")
def chat_context_set(body: ChatContextBody):
    db.set_chat_context(body.session_id or "default", body.markdown or "")
    return {"ok": True, "length": len(body.markdown or "")}


@app.delete("/api/chat/context")
def chat_context_clear(session_id: str = "default"):
    db.set_chat_context(session_id, "")
    return {"ok": True}


@app.on_event("startup")
def _voice_startup_warmup():
    try:
        voice.warmup()
    except Exception:
        pass


@app.get("/api/voice/status")
def voice_status():
    return voice.status()


@app.post("/api/voice/start")
def voice_start():
    try:
        return voice.start()
    except Exception as exc:
        raise HTTPException(400, str(exc))


@app.get("/api/voice/partial")
def voice_partial():
    return voice.partial()


@app.post("/api/voice/stop")
def voice_stop():
    try:
        return voice.stop()
    except Exception as exc:
        raise HTTPException(400, str(exc))


@app.post("/api/voice/toggle")
def voice_toggle(body: VoiceToggleBody):
    return voice.toggle(body.enabled)


@app.get("/api/voice/models")
def voice_models():
    c = (load_config().get("voice") or {}).get("correct") or {}
    base = str(c.get("base_url") or "http://127.0.0.1:11434").rstrip("/")
    try:
        tags = requests.get(base + "/api/tags", timeout=4).json()
        ps = requests.get(base + "/api/ps", timeout=4).json()
    except Exception as exc:
        raise HTTPException(400, f"Ollama недоступен: {exc}")
    loaded = {str(m.get("name") or ""): m for m in (ps.get("models") or [])}
    out = []
    for m in tags.get("models") or []:
        name = str(m.get("name") or "")
        det = m.get("details") or {}
        lm = loaded.get(name) or {}
        out.append({
            "name": name,
            "size_mb": int(int(m.get("size") or 0) / 1048576),
            "vram_mb": int(int(lm.get("size_vram") or 0) / 1048576),
            "loaded": bool(lm),
            "params": det.get("parameter_size"),
            "quant": det.get("quantization_level"),
        })
    out.sort(key=lambda x: x.get("size_mb") or 0)
    return {"base_url": base, "current": c.get("model"), "models": out}


@app.post("/api/voice/models/unload")
def voice_models_unload(body: VoiceModelBody | None = None):
    try:
        return voice.engine.unload_model((body.model if body else None) or None)
    except Exception as exc:
        raise HTTPException(400, str(exc))


@app.post("/api/voice/transcribe")
async def voice_transcribe(file: UploadFile = File(...)):
    import tempfile
    raw = await file.read()
    if not raw:
        raise HTTPException(400, "empty file")
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as fh:
        fh.write(raw)
        path = fh.name
    try:
        return voice.engine.transcribe_file(path)
    except Exception as exc:
        raise HTTPException(400, str(exc))


@app.post("/api/voice/correct/test")
def voice_correct_test():
    try:
        return voice.engine.test_correct()
    except Exception as exc:
        raise HTTPException(400, str(exc))
    finally:
        try:
            Path(path).unlink(missing_ok=True)
        except Exception:
            pass


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


@app.post("/api/chat/jobs/{job_id}/stop")
def chat_job_stop(job_id: str):
    job = stop_chat_job(job_id)
    if not job:
        raise HTTPException(404, "job not found")
    return job


@app.post("/api/chat/upload")
async def chat_upload(file: UploadFile = File(...)):
    from PIL import Image
    raw = await file.read()
    if not raw:
        raise HTTPException(400, "empty file")
    original = Path(file.filename or "file").name or "file"
    safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", original).strip("._") or "file"
    mime = (file.content_type or mimetypes.guess_type(safe_name)[0]
            or "application/octet-stream")
    kind = "file"
    stored = ""
    dst: Path | None = None
    if mime.startswith("image/"):
        # Raster images are normalised to PNG for reliable previews; anything
        # Pillow cannot decode (SVG, AVIF without a plugin, etc.) is kept as is.
        tmp = CHAT_UPLOADS / f"{uuid.uuid4().hex[:12]}.upload"
        tmp.write_bytes(raw)
        try:
            im = Image.open(tmp).convert("RGB")
            stored = f"{uuid.uuid4().hex[:12]}.png"
            dst = CHAT_UPLOADS / stored
            im.save(dst)
            mime = "image/png"
            kind = "image"
        except Exception:
            suffix = Path(safe_name).suffix[:12]
            stored = f"{uuid.uuid4().hex[:12]}{suffix}"
            dst = CHAT_UPLOADS / stored
            dst.write_bytes(raw)
        finally:
            tmp.unlink(missing_ok=True)
    if dst is None:
        suffix = Path(safe_name).suffix[:12]
        stored = f"{uuid.uuid4().hex[:12]}{suffix}"
        dst = CHAT_UPLOADS / stored
        dst.write_bytes(raw)
    return {"ok": True, "name": original, "stored": stored,
            "path": str(dst), "url": f"/api/chat/file/uploads/{stored}",
            "mime": mime, "size": len(raw), "kind": kind}


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
    return {"last_scan": db.get_meta("last_scan"),
            "last_event_sync": db.get_meta("last_event_sync"),
            "db": str(db.DB_PATH), "data": str(DATA_DIR)}
