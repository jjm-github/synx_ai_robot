from __future__ import annotations

import json
import re
import shutil
import time
from pathlib import Path
from typing import Any, Callable

from PIL import Image

import db
from config import DATA_DIR, load_bots, load_config
from links import version_url
from paths import render_dir, task_work_dir
from synx import SynxClient, SynxError, bot_client
from synx_tools import find_version, prepare_reference
from workflows import flux3_i2v, flux3_t2i


def _safe(value: object, fallback: str = "task") -> str:
    text = re.sub(r"[^A-Za-z0-9_]+", "_", str(value or "")).strip("_")
    return (text[:48] or fallback)


def last_generated_images(session_id: str, limit_messages: int = 30) -> list[dict[str, Any]]:
    """Images from the latest assistant message that produced any."""
    rows = db.all(
        "SELECT generated_json FROM chat_messages WHERE session_id=? AND role='assistant' "
        "ORDER BY id DESC LIMIT ?", (session_id, int(limit_messages)))
    for row in rows:
        try:
            images = json.loads(row.get("generated_json") or "[]")
        except Exception:
            images = []
        if images:
            return images
    return []


def _valid_images(images: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for item in images or []:
        p = Path(str(item.get("path") or ""))
        if p.is_file():
            it = dict(item)
            it["path"] = str(p)
            out.append(it)
    return out


def make_pdf(image_paths: list[str | Path], out_path: str | Path) -> Path:
    pages: list[Image.Image] = []
    for raw in image_paths:
        p = Path(raw)
        if not p.is_file():
            continue
        with Image.open(p) as im:
            pages.append(im.convert("RGB").copy())
    if not pages:
        raise ValueError("no images for PDF")
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    pages[0].save(out, "PDF", save_all=True, append_images=pages[1:], resolution=72.0)
    return out


def _pick_bot(client: SynxClient, task_id: str, requested: str | None = None
              ) -> tuple[dict[str, Any], dict[str, Any]]:
    bots = [b for b in load_bots() if b.get("enabled", True) and b.get("login") and b.get("password")]
    if not bots:
        raise ValueError("в настройках нет включённых ботов с паролем")
    try:
        users = {u.get("short_name"): u for u in client.assignment_users(task_id)}
    except Exception:
        users = {}
    if requested:
        bot = next((b for b in bots if b.get("login") == requested), None)
        if not bot:
            names = ", ".join(str(b.get("login")) for b in bots)
            raise ValueError(f"бот {requested} не настроен. Доступные боты: {names}")
        user = users.get(requested)
        if not user:
            raise ValueError(f"бот {requested} не доступен для назначения в synx")
        return bot, user
    scored = []
    for index, bot in enumerate(bots):
        user = users.get(bot.get("login"))
        if not user:
            continue
        skills = [s.strip().lower() for s in str(user.get("skills") or "").split(",") if s.strip()]
        score = 0
        if "render" in skills:
            score += 3
        if "ai" in skills:
            score += 2
        if "art" in skills:
            score += 2
        if "pipeline" in skills:
            score += 1
        scored.append((score, -index, bot, user))
    if not scored:
        raise ValueError("ни один настроенный бот не доступен для назначения в synx")
    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
    return scored[0][2], scored[0][3]


def list_bots(project: str | None = None, task_id: str | None = None) -> dict[str, Any]:
    cfg = load_config()
    client = SynxClient(cfg["synx"]["base_url"], cfg["synx"]["login"], cfg["synx"]["password"])
    users: dict[str, dict[str, Any]] = {}
    try:
        if task_id:
            users = {u.get("short_name"): u for u in client.assignment_users(task_id)}
        elif project:
            project_row = next((p for p in client.projects()
                                if project in (p.get("id"), p.get("alias"), p.get("name"))), None)
            if project_row:
                tasks = client.project_tasks(project_row["id"])
                if tasks:
                    users = {u.get("short_name"): u for u in client.assignment_users(tasks[-1]["id"])}
        elif project is None:
            tasks = client.project_tasks(client.projects()[0]["id"]) if client.projects() else []
            if tasks:
                users = {u.get("short_name"): u for u in client.assignment_users(tasks[-1]["id"])}
    except Exception:
        users = {}
    out = []
    for bot in load_bots():
        user = users.get(bot.get("login")) or {}
        out.append({
            "login": bot.get("login"),
            "full_name": user.get("full_name") or bot.get("login"),
            "skills": user.get("skills") or bot.get("role") or "",
            "enabled": bool(bot.get("enabled", True)),
            "has_password": bool(bot.get("password")),
            "available_in_synx": bool(user),
        })
    return {"bots": out}


def _workflow_for_item(item: dict[str, Any], ref_name: str, cfg: dict[str, Any],
                       width: int, height: int, index: int) -> dict[str, Any]:
    raw = item.get("workflow_json")
    if isinstance(raw, dict):
        return raw
    profile = str(item.get("workflow") or "flux3_i2v").lower()
    prompt = str(item.get("prompt") or "Cubist restyle of the source frame; preserve composition and aspect ratio")
    steps = int(item.get("steps") or (8 if "i2v" in profile else 24))
    cfg_value = float(item.get("cfg") if item.get("cfg") is not None else (1.0 if "i2v" in profile else 2.0))
    denoise = float(item.get("denoise") if item.get("denoise") is not None else 1.0)
    models = item.get("models") or {}
    seed = int(item.get("seed") or (20261117 + index))
    if "i2v" in profile:
        return flux3_i2v(prompt, "", ref_name, width, height, seed, cfg,
                         steps=steps, cfg_value=cfg_value, denoise=denoise,
                         megapixels=(width * height) / 1_000_000.0, models=models)
    return flux3_t2i(prompt, "", width, height, seed, cfg,
                     steps=steps, cfg_value=cfg_value, models=models)


def _infer_target(session_id: str, project: str | None, shot: str | None,
                  version: str | None) -> tuple[str, str, str | None]:
    if shot:
        return (project or "", shot, version)
    rows = db.all(
        "SELECT content FROM chat_messages WHERE session_id=? ORDER BY id DESC LIMIT 30",
        (session_id,))
    text = "\n".join(str(r.get("content") or "") for r in rows)
    m_shot = re.search(r"\b([A-Za-z][A-Za-z0-9_]*_\d{3,5})\b", text)
    if not m_shot:
        raise ValueError("не понял, к какому шоту относятся кадры — укажи project и shot")
    found_shot = m_shot.group(1)
    m_ver = re.search(r"\b(v\d{1,4})\b", text)
    found_version = version or (m_ver.group(1) if m_ver else None)
    found_project = project
    if not found_project:
        aliases = []
        cfg = load_config()
        try:
            c = SynxClient(cfg["synx"]["base_url"], cfg["synx"]["login"], cfg["synx"]["password"])
            aliases = [str(p.get("alias")) for p in c.projects() if p.get("alias")]
        except Exception:
            aliases = []
        low = text.lower()
        found_project = next((a for a in aliases if a.lower() in low), "")
    return found_project, found_shot, found_version


def publish_images_as_version(project: str | None, shot: str | None,
                              version: str | None,
                              images: list[dict[str, Any]] | None = None,
                              session_id: str = "default",
                              task_name: str = "cubism_frames",
                              task_type: str = "Art",
                              bot_login: str | None = None,
                              comment: str = "",
                              version_comment: str = "",
                              pdf_name: str = "",
                              dry_run: bool = False,
                              progress: Callable[[dict[str, Any]], None] | None = None
                              ) -> dict[str, Any]:
    def stage(text: str) -> None:
        if progress:
            progress({"type": "stage", "text": text})

    source_images = _valid_images(images or last_generated_images(session_id))
    if not source_images:
        raise ValueError("в чате нет готовых изображений для PDF")
    project, shot, version = _infer_target(session_id, project, shot, version)
    stage("Ищу шот и версию в synx...")
    info = find_version(project, shot, version)
    project_alias = info.get("project") or project
    project_id = info.get("project_id")
    entity_id = info.get("entity_id")
    entity_name = info.get("entity") or shot
    if not project_id or not entity_id:
        raise ValueError("не удалось определить проект/сущность версии")
    cfg = load_config()
    client = SynxClient(cfg["synx"]["base_url"], cfg["synx"]["login"], cfg["synx"]["password"])
    entity = next((e for e in client.entities(project_id) if e.get("id") == entity_id), {})
    sequence = entity.get("sequence_name") or ""
    entity_kind = entity.get("entity_kind") or info.get("entity_kind") or "shot"
    name = _safe(task_name, "cubism_frames")
    requested_type = task_type or "Art"
    task_types = client.task_types(project_id)
    if task_types and requested_type not in task_types:
        requested_type = "Art" if "Art" in task_types else task_types[0]
    versions = client.versions(entity_id)
    next_ver = max([int(v.get("version_number") or 0) for v in versions] + [0]) + 1
    total = len(source_images)
    w0 = int(source_images[0].get("width") or 0)
    h0 = int(source_images[0].get("height") or 0)
    if not w0 or not h0:
        with Image.open(source_images[0]["path"]) as im:
            w0, h0 = im.size

    if dry_run:
        out_dir = DATA_DIR / "chat" / "publish_dry" / f"dry_{int(time.time())}"
        out_dir.mkdir(parents=True, exist_ok=True)
        local = []
        for i, item in enumerate(source_images, 1):
            dst = out_dir / f"frame_{i:02d}.png"
            shutil.copy2(item["path"], dst)
            local.append(dst)
        pdf = make_pdf(local, out_dir / (_safe(pdf_name, name) + ".pdf"))
        return {"ok": True, "dry_run": True, "project": project_alias, "shot": entity_name,
                "source_version": info.get("version"), "target_version": next_ver,
                "images": [str(p) for p in local], "pdf": str(pdf),
                "task_name": name, "task_type": requested_type, "bot": bot_login or "auto",
                "project_corrected": bool(info.get("project_corrected")),
                "requested_project": info.get("requested_project")}

    stage("Создаю задачу в synx...")
    try:
        task = client.create_task(entity_id, name, requested_type,
                                  comment=comment or f"PDF из {total} кадров по версии {info.get('version')}",
                                  bid=3.0, status="wip")
    except SynxError as exc:
        if "409" not in str(exc):
            raise
        task = next((t for t in client.tasks(entity_id) if t.get("task_name") == name), None)
        if not task:
            raise
    task_id = task.get("id")
    stage("Выбираю бота по скилам synx...")
    bot, user = _pick_bot(client, task_id, bot_login)
    bot_name = bot.get("login")
    try:
        client.assign(task_id, [user.get("id")])
    except Exception as exc:
        raise SynxError(f"не удалось назначить бота {bot_name}: {exc}") from exc

    stage("Готовлю рабочую папку задачи...")
    work = task_work_dir(cfg, project_alias, entity_name, sequence, requested_type,
                         bot_name, name, next_ver, entity_kind=entity_kind)
    renders = render_dir(cfg, project_alias, entity_name, sequence, next_ver,
                         entity_kind=entity_kind)
    work.mkdir(parents=True, exist_ok=True)
    renders.mkdir(parents=True, exist_ok=True)
    wf_dir = work / "workflows"
    wf_dir.mkdir(parents=True, exist_ok=True)

    copied = []
    for i, item in enumerate(source_images, 1):
        dst = work / f"frame_{i:02d}.png"
        shutil.copy2(item["path"], dst)
        copied.append(dst)
        shutil.copy2(dst, renders / dst.name)

    stage("Скачиваю исходный референс...")
    ref_name = "source_reference.png"
    try:
        ref = prepare_reference(project_alias, entity_name, info.get("version"), frame="middle")
        shutil.copy2(ref["path"], work / ref_name)
    except Exception:
        ref = {}

    stage("Сохраняю workflow-файлы...")
    workflow_files = []
    meta_images = []
    for i, (item, local) in enumerate(zip(source_images, copied), 1):
        w = int(item.get("width") or 0)
        h = int(item.get("height") or 0)
        if not w or not h:
            with Image.open(local) as im:
                w, h = im.size
        profile = str(item.get("workflow") or "flux3_i2v")
        wf = _workflow_for_item(item, ref_name, cfg, w, h, i)
        wf_path = wf_dir / f"workflow_{i:02d}_{_safe(profile, 'workflow')}.json"
        wf_path.write_text(json.dumps(wf, ensure_ascii=False, indent=2), encoding="utf-8")
        workflow_files.append(wf_path)
        meta_images.append({
            "index": i,
            "file": local.name,
            "width": w,
            "height": h,
            "workflow": profile,
            "workflow_file": str(wf_path.relative_to(work)),
            "prompt": item.get("prompt") or "",
        })
    meta = {
        "source": {
            "project": project_alias,
            "shot": entity_name,
            "version": info.get("version"),
            "version_id": info.get("id"),
            "media_kind": info.get("media_kind"),
            "width": info.get("width"),
            "height": info.get("height"),
        },
        "task": {"synx_task_id": task_id, "name": name, "type": requested_type,
                 "bot": bot_name, "bot_skills": user.get("skills") or ""},
        "images": meta_images,
        "pdf": _safe(pdf_name, name) + ".pdf",
    }
    (work / "generation_meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2),
                                               encoding="utf-8")

    stage("Собираю PDF: одна картинка на страницу...")
    pdf = make_pdf(copied, work / (_safe(pdf_name, name) + ".pdf"))
    shutil.copy2(pdf, renders / pdf.name)
    version_cfg = {
        "profile": "document",
        "source_version": info.get("version"),
        "images": total,
        "page_size": f"{w0}x{h0}",
        "workflow_files": [str(p.relative_to(work)) for p in workflow_files],
    }
    (work / "README.md").write_text(
        "# " + name + "\n\n"
        f"- Шот: {project_alias}/{entity_name}\n"
        f"- Исходная версия: {info.get('version')}\n"
        f"- Задача synx: {task_id} ({requested_type})\n"
        f"- Бот: {bot_name} ({user.get('skills') or ''})\n"
        f"- PDF: {pdf.name} — {total} страниц, по одной картинке на страницу\n"
        f"- Render: {renders}\n"
        "- Workflows: папка `workflows/`, по одному JSON на кадр.\n"
        "- `source_reference.png` — кадр исходной версии.\n"
        "- `generation_meta.json` — параметры генерации и файлов.\n",
        encoding="utf-8")

    stage("Публикую PDF от имени бота...")
    bc = bot_client(cfg["synx"]["base_url"], bot)
    v = bc.publish_version(
        entity_id, pdf, task_id,
        comment=version_comment or f"PDF: {total} кадров, по одному на страницу",
        minutes=10, note="Synx AI Robot chat PDF")
    version_code = v.get("version_code")
    url = version_url(cfg["synx"]["base_url"], project_alias, entity_kind, entity_name,
                      version_code, requested_type, name) if version_code else None
    try:
        bc.task_comment(task_id, f"PDF {version_code or ''} опубликован: {url or pdf}")
    except Exception:
        pass
    try:
        client.task_status(task_id, "done")
    except Exception:
        pass

    source = {
        "project_id": project_id, "project_alias": project_alias,
        "entity_id": entity_id, "shot": entity_name, "sequence": sequence,
        "entity_kind": entity_kind, "entity_name": entity_name,
        "version_id": info.get("id"), "version_code": info.get("version"),
        "target_version": next_ver, "width": w0, "height": h0, "frames": 1,
        "seed": 20261117, "references": [str(work / ref_name)] if ref else [],
        "workflow_config": version_cfg,
    }
    plan = {
        "needs_task": True, "summary": f"PDF из {total} кадров, публикация версии",
        "task_name": name, "task_type": requested_type, "assignee": bot_name,
        "publish_comment": version_comment or f"PDF: {total} кадров",
        "images": [str(p) for p in copied], "pdf": str(pdf),
        "workflow_config": version_cfg,
    }
    tid = db.create_task({
        "publication_id": None, "synx_task_id": task_id, "project_id": project_id,
        "entity_id": entity_id, "shot": entity_name, "task_name": name,
        "task_type": requested_type, "bot": bot_name, "status": "done",
        "stage": "published", "progress": 100, "prompt": f"PDF from {total} images",
        "negative": "", "plan_json": json.dumps(plan, ensure_ascii=False),
        "plan_md": "", "plan_md_path": "", "workflow_json": None,
        "source_json": json.dumps(source, ensure_ascii=False),
        "work_dir": str(work), "render_dir": str(renders),
        "result_file": str(pdf), "version_id": v.get("id"),
        "version_code": version_code, "synx_url": url, "priority": 0,
    })
    db.add_event(tid, "published", f"PDF {version_code} published as {url or pdf}")
    stage("Готово")
    return {
        "ok": True, "dry_run": False, "task_id": tid, "synx_task_id": task_id,
        "task_name": name, "task_type": requested_type, "bot": bot_name,
        "bot_skills": user.get("skills") or "", "project": project_alias,
        "shot": entity_name, "source_version": info.get("version"),
        "target_version": next_ver, "version_code": version_code,
        "version_id": v.get("id"), "url": url, "pdf": str(pdf),
        "work_dir": str(work), "render_dir": str(renders),
        "workflows": [str(p) for p in workflow_files],
        "images": [str(p) for p in copied], "page_size": f"{w0}x{h0}",
        "project_corrected": bool(info.get("project_corrected")),
        "requested_project": info.get("requested_project"),
    }
