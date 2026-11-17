from __future__ import annotations

import json
import re
import shutil
import time
import uuid
from datetime import date
from pathlib import Path
from typing import Any

import requests
from PIL import Image

import db
import fs_tools
import synx_tools
from ai import AIClient, AIError, _data_url
from config import ROOT, load_config
from workflows import flux3_i2v, flux3_t2i

requests.packages.urllib3.disable_warnings()

CHAT_DIR = ROOT / "data" / "chat"
UPLOADS = CHAT_DIR / "uploads"
IMAGES = CHAT_DIR / "images"
for _p in (UPLOADS, IMAGES):
    _p.mkdir(parents=True, exist_ok=True)

CHAT_SYSTEM = """Ты — ассистент Synx AI Robot. Отвечай по-русски, кратко и по делу.

Ты умеешь:
- отвечать на вопросы о производстве, synx, версиях, задачах, проектах, ComfyUI и промптах;
- анализировать прикреплённые пользователем изображения;
- ходить в synx по API с данными из настроек приложения;
- генерировать изображения в локальном ComfyUI;
- собирать плейлист Synx из опубликованных версий за период.

Инструменты synx:
- synx_projects — список проектов;
- synx_versions — версии проекта за период (project, date_from, date_to, limit);
- synx_tasks — задачи проекта;
- synx_make_playlist — готовый текст плейлиста Synx за период.

Если пользователь спрашивает про состояние проектов/версий/задач — сначала получи
данные через synx-инструмент, не выдумывай. У тебя ЕСТЬ доступ к synx через API;
если в истории чата есть старые ответы «доступа нет» — это устаревшая информация,
не повторяй её. Если просит плейлист — вызови
synx_make_playlist и укажи project и даты. Сегодняшняя дата известна из контекста.

Если пользователь просит нарисовать/сгенерировать/сделать изображение или кадр,
вызови generate_image. В prompt передай подробный английский промпт. Если в
сообщении есть прикреплённые референсы — опиши, что из них взять.

Для вопросов, не требующих инструментов, просто ответь текстом.
"""

IMAGE_TOOL = {
    "type": "function",
    "function": {
        "name": "generate_image",
        "description": "Generate an image in local ComfyUI from a text prompt and optional user reference images.",
        "parameters": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "Detailed English image prompt"},
                "negative": {"type": "string", "description": "English negative prompt"},
            },
            "required": ["prompt"],
        },
    },
}

SYNX_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "synx_projects",
            "description": "List synx projects.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "synx_versions",
            "description": "List published versions of a synx project for a date period.",
            "parameters": {
                "type": "object",
                "properties": {
                    "project": {"type": "string", "description": "Project alias, name or id"},
                    "date_from": {"type": "string", "description": "YYYY-MM-DD inclusive"},
                    "date_to": {"type": "string", "description": "YYYY-MM-DD inclusive"},
                    "limit": {"type": "integer", "default": 300},
                    "playable_only": {"type": "boolean", "default": False},
                },
                "required": ["project"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "synx_tasks",
            "description": "List tasks of a synx project, optionally filtered by status.",
            "parameters": {
                "type": "object",
                "properties": {
                    "project": {"type": "string"},
                    "status": {"type": "string"},
                    "limit": {"type": "integer", "default": 300},
                },
                "required": ["project"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "synx_make_playlist",
            "description": "Build a Synx Player playlist text from versions published in a period.",
            "parameters": {
                "type": "object",
                "properties": {
                    "project": {"type": "string"},
                    "date_from": {"type": "string"},
                    "date_to": {"type": "string"},
                    "limit": {"type": "integer", "default": 500},
                    "playable_only": {"type": "boolean", "default": True},
                },
                "required": ["project"],
            },
        },
    },
]

FS_TOOLS = [
    {"type": "function", "function": {
        "name": "fs_list",
        "description": "List allowed filesystem roots or a directory inside them.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "Empty for roots, or absolute path inside an allowed root"}
        }},
    }},
    {"type": "function", "function": {
        "name": "fs_read",
        "description": "Read a text file or binary file inside an allowed root.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"},
            "max_bytes": {"type": "integer", "default": 300000}
        }, "required": ["path"]},
    }},
    {"type": "function", "function": {
        "name": "fs_write",
        "description": "Write a file inside an allowed root. Full access inside configured roots.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"},
            "content": {"type": "string"},
            "base64_data": {"type": "boolean", "default": False}
        }, "required": ["path", "content"]},
    }},
    {"type": "function", "function": {
        "name": "fs_delete",
        "description": "Delete a file or directory inside an allowed root.",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
    }},
]

TOOLS = [IMAGE_TOOL] + SYNX_TOOLS + FS_TOOLS
GEN_RE = re.compile(
    r"(нарисуй|сгенер|сгенерируй|сделай\s+(картин|изображ|кадр|арт)|"
    r"draw|generate\s+(an?\s+)?image|make\s+(an?\s+)?image|create\s+(an?\s+)?image)",
    re.I,
)
PLAYLIST_RE = re.compile(r"(плейлист|playlist)", re.I)


def _synx_guide() -> str:
    p = ROOT / "prompts" / "synx_api.md"
    try:
        return p.read_text(encoding="utf-8")
    except Exception:
        return ""


def _system() -> str:
    roots = "\n".join("- " + r for r in fs_tools.roots()) or "- (нет папок)"
    return (CHAT_SYSTEM + f"\n\nСегодня: {date.today().isoformat()}.\n\n"
            "## Synx API\n\n" + _synx_guide() +
            "\n\n## Рабочие папки (полный доступ)\n" + roots)


def _refs_for_generation(images: list[dict[str, Any]]) -> list[str]:
    out = []
    for im in images or []:
        p = im.get("path") if isinstance(im, dict) else None
        if p and Path(p).is_file():
            out.append(str(p))
    return out


def _stage_refs(refs: list[str], input_dir: Path) -> list[str]:
    input_dir.mkdir(parents=True, exist_ok=True)
    names = []
    for i, p in enumerate(refs[:4]):
        src = Path(p)
        name = f"chat_ref_{uuid.uuid4().hex[:8]}_{i}{src.suffix.lower() or '.png'}"
        shutil.copy2(src, input_dir / name)
        names.append(name)
    return names


def _submit(workflow: dict[str, Any], cfg: dict[str, Any], out_dir: Path,
            progress=None) -> list[Path]:
    if progress:
        progress({"type": "stage", "text": "Отправляю workflow в ComfyUI..."})
    base = str(cfg["comfy"].get("base_url") or "http://127.0.0.1:8199").rstrip("/")
    r = requests.post(f"{base}/prompt", json={"prompt": workflow,
                      "client_id": f"chat-{uuid.uuid4().hex[:8]}"}, timeout=60)
    if r.status_code >= 400:
        raise AIError(f"ComfyUI /prompt {r.status_code}: {r.text[:1200]}")
    pid = r.json().get("prompt_id")
    deadline = time.time() + int(cfg["comfy"].get("timeout_sec", 1800))
    out_dir.mkdir(parents=True, exist_ok=True)
    files: list[Path] = []
    if progress:
        progress({"type": "stage", "text": "Жду генерации в ComfyUI..."})
    while time.time() < deadline:
        time.sleep(2)
        h = requests.get(f"{base}/history/{pid}", timeout=30).json()
        item = h.get(pid)
        if not item:
            continue
        st = item.get("status") or {}
        if st.get("status_str") == "error":
            raise AIError("ComfyUI error: " + json.dumps(st)[:1200])
        if st.get("completed") or st.get("status_str") == "success":
            for out in (item.get("outputs") or {}).values():
                for key in ("images", "gifs", "videos"):
                    for f in out.get(key) or []:
                        name = f.get("filename")
                        if not name:
                            continue
                        rr = requests.get(f"{base}/view", params={
                            "filename": name, "subfolder": f.get("subfolder") or "",
                            "type": f.get("type", "output"),
                        }, timeout=300)
                        if rr.status_code == 200:
                            p = out_dir / f"{uuid.uuid4().hex[:10]}_{name}"
                            p.write_bytes(rr.content)
                            files.append(p)
            break
    if not files:
        raise AIError("ComfyUI завершился без изображения")
    return files


def generate_image(prompt: str, negative: str = "", images: list[dict[str, Any]] | None = None,
                   progress=None) -> list[dict[str, Any]]:
    if progress:
        progress({"type": "stage", "text": "Готовлю референсы..."})
    cfg = load_config()
    refs = _refs_for_generation(images or [])
    input_dir = Path(str(cfg["comfy"].get("input_dir") or "~/opt/ComfyUI/input")).expanduser()
    names = _stage_refs(refs, input_dir)
    width = height = 1024
    seed = 20261117
    if progress:
        progress({"type": "stage", "text": "Собираю workflow FLUX.3..."})
    if names:
        wf = flux3_i2v(prompt, negative, names, width, height, seed, cfg, steps=8)
    else:
        wf = flux3_t2i(prompt, negative, width, height, seed, cfg, steps=24)
    files = _submit(wf, cfg, IMAGES, progress=progress)
    result = []
    for p in files:
        dst = IMAGES / f"{uuid.uuid4().hex[:12]}.png"
        try:
            Image.open(p).convert("RGB").save(dst)
        except Exception:
            shutil.copy2(p, dst)
        item = {"name": dst.name, "path": str(dst), "url": f"/api/chat/file/images/{dst.name}"}
        result.append(item)
        if progress:
            progress({"type": "image", "text": "Изображение готово", "image": item})
    return result


def _image_content(images: list[dict[str, Any]]) -> list[dict[str, Any]]:
    parts = []
    for im in images or []:
        p = im.get("path") if isinstance(im, dict) else None
        if p and Path(p).is_file():
            try:
                parts.append({"type": "image_url", "image_url": {"url": _data_url(Path(p))}})
            except Exception:
                pass
    return parts


def _history_messages(session_id: str) -> list[dict[str, Any]]:
    rows = db.list_chat_messages(session_id, limit=40)
    out = [{"role": "system", "content": _system()}]
    for r in rows:
        if r.get("role") == "user" and r.get("images"):
            content: list[dict[str, Any]] = [{"type": "text", "text": r.get("content") or ""}]
            content.extend(_image_content(r["images"]))
            out.append({"role": "user", "content": content})
        else:
            out.append({"role": r.get("role") or "assistant", "content": r.get("content") or ""})
    return out


def _execute_tool(name: str, args: dict[str, Any], images: list[dict[str, Any]],
                  progress=None) -> tuple[Any, list[dict[str, Any]], str | None]:
    """Returns (tool_result, generated_images, playlist_text)."""
    if name == "generate_image":
        prompt = (args.get("prompt") or "").strip()
        neg = args.get("negative") or ""
        generated = generate_image(prompt, neg, images, progress=progress)
        return {"ok": True, "images": generated}, generated, None
    if name == "synx_projects":
        return synx_tools.projects(), [], None
    if name == "synx_versions":
        return synx_tools.versions(args.get("project") or "", args.get("date_from"),
                                   args.get("date_to"), int(args.get("limit") or 300),
                                   bool(args.get("playable_only"))), [], None
    if name == "synx_tasks":
        return synx_tools.tasks(args.get("project") or "", args.get("status"),
                                int(args.get("limit") or 300)), [], None
    if name == "synx_make_playlist":
        d = synx_tools.make_playlist(args.get("project") or "", args.get("date_from"),
                                     args.get("date_to"), int(args.get("limit") or 500),
                                     bool(args.get("playable_only", True)))
        return {"ok": True, "count": d["count"], "from": d["from"], "to": d["to"],
                "items": d["items"]}, [], d["text"]
    if name == "fs_list":
        return fs_tools.list_dir(args.get("path") or ""), [], None
    if name == "fs_read":
        r = fs_tools.read_file(args.get("path") or "", int(args.get("max_bytes") or 300000))
        gen = []
        if r.get("kind") == "binary" and str(r.get("mime", "")).startswith("image/"):
            from urllib.parse import quote
            gen = [{"name": Path(r["path"]).name, "path": r["path"],
                    "url": "/api/fs/file?path=" + quote(r["path"])}]
        return r, gen, None
    if name == "fs_write":
        return fs_tools.write_file(args.get("path") or "", args.get("content") or "",
                                   bool(args.get("base64_data"))), [], None
    if name == "fs_delete":
        return fs_tools.delete_path(args.get("path") or ""), [], None
    return {"error": "unknown tool " + name}, [], None


def chat_turn(session_id: str, text: str, images: list[dict[str, Any]] | None = None,
              progress=None) -> dict[str, Any]:
    images = images or []
    stages: list[dict[str, Any]] = []

    def emit(evt: dict[str, Any]):
        evt = dict(evt)
        evt["seq"] = len(stages) + 1
        stages.append(evt)
        if progress:
            progress(evt)

    emit({"type": "stage", "text": "Анализирую запрос..."})
    db.add_chat_message(session_id, "user", text, images=images)
    history = _history_messages(session_id)
    cfg = load_config()
    ai = AIClient(cfg)
    usage = {"input": 0, "output": 0}
    generated: list[dict[str, Any]] = []
    playlist_text: str | None = None
    content = ""
    tool_used = False
    ai_error = False
    try:
        for _ in range(5):
            emit({"type": "stage", "text": "Думаю..."})
            msg, u = ai.chat_message(history, tools=TOOLS)
            usage["input"] += u.get("input", 0)
            usage["output"] += u.get("output", 0)
            calls = msg.get("tool_calls") or []
            if not calls:
                content = (msg.get("content") or "").strip()
                break
            tool_used = True
            emit({"type": "stage", "text": "Вызываю инструмент..."})
            history.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": calls})
            for call in calls:
                fn = (call.get("function") or {})
                name = fn.get("name") or ""
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except Exception:
                    args = {}
                try:
                    emit({"type": "stage", "text": f"Инструмент: {name}"})
                    result, gen, pl = _execute_tool(name, args, images, progress=emit)
                    generated.extend(gen)
                    if pl:
                        playlist_text = pl
                except Exception as exc:
                    result = {"error": str(exc)}
                history.append({"role": "tool", "tool_call_id": call.get("id") or name,
                                "content": json.dumps(result, ensure_ascii=False)[:12000]})
        else:
            content = "Слишком много шагов инструментов. Уточни запрос."
    except AIError as exc:
        ai_error = True
        content = f"AI недоступен: {exc}"
    if not content and playlist_text:
        content = "Готово. Плейлист сформирован."
    if not content and generated:
        content = "Готово. Сгенерировал изображение."
    if (ai_error or not tool_used) and PLAYLIST_RE.search(text or ""):
        try:
            d = synx_tools.make_playlist(load_config()["synx"]["projects"][0])
            playlist_text = d["text"]
            content = f"Готово. Плейлист из {d['count']} версий."
        except Exception as exc:
            if ai_error and not content:
                content = f"Не удалось собрать плейлист: {exc}"
    elif (ai_error or not tool_used) and GEN_RE.search(text or ""):
        try:
            generated = generate_image(text.strip(), "", images)
            content = "Готово. Сгенерировал изображение по твоему запросу."
        except Exception as exc:
            if ai_error and not content:
                content = f"Не удалось сгенерировать изображение: {exc}"
    if not content and not tool_used:
        if PLAYLIST_RE.search(text or ""):
            try:
                d = synx_tools.make_playlist(load_config()["synx"]["projects"][0])
                playlist_text = d["text"]
                content = f"Готово. Плейлист из {d['count']} версий."
            except Exception as exc:
                content = f"Не удалось собрать плейлист: {exc}"
        elif GEN_RE.search(text or ""):
            try:
                generated = generate_image(text.strip(), "", images)
                content = "Готово. Сгенерировал изображение по твоему запросу."
            except Exception as exc:
                content = f"Не удалось сгенерировать изображение: {exc}"
        elif not content:
            content = "Не смог получить ответ от модели. Проверь настройки AI."
    emit({"type": "stage", "text": "Готово"})
    db.add_chat_message(session_id, "assistant", content, generated=generated,
                        usage=usage, playlist_text=playlist_text, progress=stages)
    return {"messages": db.list_chat_messages(session_id), "usage": usage,
            "generated": generated, "playlist_text": playlist_text, "stages": stages}


# --- background chat jobs for live progress ---------------------------------
import threading

_JOBS: dict[str, dict[str, Any]] = {}
_JOBS_LOCK = threading.Lock()


def start_chat_job(session_id: str, text: str, images: list[dict[str, Any]]) -> str:
    job_id = uuid.uuid4().hex[:16]
    job = {"id": job_id, "session_id": session_id, "status": "running",
           "events": [], "result": None, "error": None}
    with _JOBS_LOCK:
        _JOBS[job_id] = job

    def progress(evt: dict[str, Any]):
        with _JOBS_LOCK:
            job["events"].append(evt)

    def run():
        try:
            result = chat_turn(session_id, text, images, progress=progress)
            with _JOBS_LOCK:
                job["result"] = result
                job["status"] = "done"
        except Exception as exc:
            with _JOBS_LOCK:
                job["error"] = str(exc)
                job["status"] = "error"

    threading.Thread(target=run, daemon=True, name="chat-job").start()
    return job_id


def get_chat_job(job_id: str) -> dict[str, Any] | None:
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
        if not job:
            return None
        return {"id": job["id"], "status": job["status"], "events": list(job["events"]),
                "error": job["error"],
                "result": job["result"]}
