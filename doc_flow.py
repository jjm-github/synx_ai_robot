"""Publishing of text documents as synx versions.

Text deliverables (scripts, plans, .md) become a *vector text PDF* — real
characters, embedded fonts, white pages — never a bitmap render. The source
.md and any extra files are attached to the version comment.
"""
from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Any, Callable

import db
import text_pdf
from config import load_config
from links import version_url
from paths import render_dir, task_work_dir
from publish_flow import _pick_bot
from synx import SynxClient, bot_client


def _safe(value: str, fallback: str = "document") -> str:
    out = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or "")).strip("_")
    return out or fallback


def _slug(title: str) -> str:
    out = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(title or "")).strip("_")
    out = re.sub(r"_+", "_", out)
    return out[:60] or "document"


def _resolve(client: SynxClient, project: str, entity: str
             ) -> tuple[dict[str, Any], dict[str, Any]]:
    projects = client.projects()
    proj = next((p for p in projects
                 if str(p.get("alias") or "").lower() == str(project).lower()
                 or str(p.get("name") or "").lower() == str(project).lower()), None)
    if not proj:
        names = ", ".join(str(p.get("alias")) for p in projects)
        raise ValueError(f"проект не найден: {project}. Доступные: {names}")
    rows = client.entities(proj["id"])
    ent = next((e for e in rows
                if str(e.get("name") or "").lower() == str(entity).lower()), None)
    if not ent:
        sample = ", ".join(str(e.get("name")) for e in rows[:20])
        raise ValueError(f"сущность не найдена: {entity} (например: {sample})")
    return proj, ent


def publish_text_document(project: str, entity: str, *,
                          text: str | None = None,
                          pdf_path: str | Path | None = None,
                          title: str | None = None,
                          task_name: str | None = None,
                          task_type: str | None = None,
                          bot_login: str | None = None,
                          comment: str = "",
                          attach_files: list[str | Path] | None = None,
                          page_size: str = "A4",
                          font_size: float = 10.5,
                          progress: Callable[[dict[str, Any]], None] | None = None
                          ) -> dict[str, Any]:
    def stage(msg: str) -> None:
        if progress:
            progress({"type": "stage", "text": msg})

    if not text and not pdf_path:
        raise ValueError("нужен text (markdown) или pdf_path")

    cfg = load_config()
    client = SynxClient(cfg["synx"]["base_url"], cfg["synx"]["login"], cfg["synx"]["password"])
    proj, ent = _resolve(client, project, entity)
    alias = str(proj.get("alias") or project)
    entity_name = str(ent.get("name") or entity)
    entity_kind = str(ent.get("entity_kind") or "asset")
    sequence = str(ent.get("sequence_name") or "")

    types = client.task_types(proj["id"])
    requested_type = task_type or ("Script" if "Script" in types else "Art")
    if types and requested_type not in types:
        requested_type = "Script" if "Script" in types else (types[0] if types else "Art")
    name = task_name or _safe(title or Path(str(pdf_path or entity_name)).stem, "text_document")

    tasks = client.tasks(ent["id"])
    task = next((t for t in tasks if t.get("task_name") == name), None)
    if task and task.get("task_type"):
        requested_type = str(task["task_type"])
    if not task:
        stage("Создаю задачу в synx...")
        task = client.create_task(ent["id"], name, requested_type,
                                  comment=comment or f"Текстовый документ: {title or name}",
                                  bid=3.0, status="wip")
    task_id = task.get("id")

    stage("Выбираю бота по скилам...")
    bot, user = _pick_bot(client, task_id, bot_login)
    bot_name = bot.get("login")
    try:
        client.assign(task_id, [user.get("id")])
    except Exception:
        pass

    versions = client.versions(ent["id"])
    next_ver = max([int(v.get("version_number") or 0) for v in versions] + [0]) + 1
    work = task_work_dir(cfg, alias, entity_name, sequence, requested_type,
                         bot_name, name, next_ver, entity_kind=entity_kind)
    render = render_dir(cfg, alias, entity_name, sequence, next_ver, entity_kind=entity_kind)
    work.mkdir(parents=True, exist_ok=True)
    render.mkdir(parents=True, exist_ok=True)

    doc_title = title or name
    stem = f"{_safe(name)}_{next_ver:03d}"
    if pdf_path:
        pdf = Path(pdf_path).expanduser()
        if not pdf.is_file():
            raise ValueError(f"нет PDF: {pdf}")
        target_pdf = work / (stem + pdf.suffix.lower())
        if pdf.resolve() != target_pdf.resolve():
            shutil.copy2(pdf, target_pdf)
        pdf = target_pdf
    else:
        stage("Собираю текстовый PDF (векторный текст, белый фон)...")
        pdf = work / (stem + ".pdf")
        info = text_pdf.make_text_pdf(text or "", pdf, title=doc_title,
                                      page_size=page_size, font_size=font_size)
        if not info.get("ok"):
            raise ValueError("текстовый PDF не прошёл проверку: " +
                             "; ".join(info.get("errors") or []))

    check = text_pdf.verify_text_pdf(pdf)
    if not check.get("ok"):
        raise ValueError("PDF не текстовый: " + "; ".join(check.get("errors") or []))
    stage(f"PDF проверен: {check.get('pages')} стр., текст извлекается "
          f"({check.get('text_chars')} символов), шрифтов: {check.get('fonts')}")

    md_path: Path | None = None
    if text:
        md_path = work / (stem + ".md")
        md_path.write_text(text, encoding="utf-8")
        check["markdown"] = str(md_path)

    attached: list[str] = []
    for item in attach_files or []:
        src = Path(str(item)).expanduser()
        if not src.is_file():
            continue
        dst = work / src.name
        if src.resolve() != dst.resolve():
            shutil.copy2(src, dst)
        attached.append(str(dst))

    shutil.copy2(pdf, render / pdf.name)
    meta = {
        "source": {"project": alias, "entity": entity_name,
                   "entity_kind": entity_kind, "task": name, "task_type": requested_type,
                   "task_synx_id": task_id},
        "document": {"title": doc_title, "pdf": pdf.name, "pages": check.get("pages"),
                     "text_chars": check.get("text_chars"), "fonts": check.get("fonts"),
                     "markdown": md_path.name if md_path else "",
                     "attached": [Path(a).name for a in attached]},
        "bot": bot_name,
    }
    (work / "generation_meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2),
                                               encoding="utf-8")
    (work / "README.md").write_text(
        f"# {doc_title}\n\n- Проект: {alias}\n- Сущность: {entity_name} ({entity_kind})\n"
        f"- Задача synx: {task_id} ({name}, {requested_type})\n- Бот: {bot_name}\n"
        f"- PDF: {pdf.name} — {check.get('pages')} стр., текст извлекается, "
        f"шрифтов: {check.get('fonts')}\n"
        + (f"- Markdown: {md_path.name}\n" if md_path else "")
        + (f"- Вложения: {', '.join(Path(a).name for a in attached)}\n" if attached else "")
        + f"- Render: {render}\n", encoding="utf-8")

    stage("Публикую версию от имени бота...")
    bc = bot_client(cfg["synx"]["base_url"], bot)
    v = bc.publish_version(ent["id"], pdf, task_id,
                           comment=(comment or f"{doc_title}: текстовый PDF, "
                                    f"{check.get('pages')} стр."),
                           minutes=10, note="Synx AI Robot text document")
    vcode = v.get("version_code")
    media = v.get("media") or {}
    url = version_url(cfg["synx"]["base_url"], alias, entity_kind, entity_name,
                      vcode, requested_type, name) if vcode else None

    files_to_attach = ([str(md_path)] if md_path else []) + attached
    publication = None
    if files_to_attach:
        stage("Прикрепляю файлы к задаче (публикация)...")
        try:
            # synx comments accept images only; text files are attached as a
            # task publication, which is the supported way for .md/.txt/.json.
            publication = bc.create_publication(
                task_id, files_to_attach,
                comment=f"Исходные файлы: {doc_title} ({vcode or ''})",
                note="Synx AI Robot text document")
        except Exception as exc:
            check["attach_error"] = str(exc)
    if url:
        try:
            bc.task_comment(task_id, f"{vcode}: {doc_title}\n{url}")
        except Exception:
            pass
    try:
        client.task_status(task_id, "done")
    except Exception:
        pass

    tid = db.create_task({
        "publication_id": None, "synx_task_id": task_id, "project_id": proj["id"],
        "entity_id": ent["id"], "shot": entity_name, "task_name": name,
        "task_type": requested_type, "bot": bot_name, "status": "done",
        "stage": "published", "progress": 100, "prompt": doc_title,
        "negative": "", "plan_json": json.dumps(meta, ensure_ascii=False),
        "plan_md": "", "plan_md_path": "", "workflow_json": None,
        "source_json": json.dumps(meta["source"], ensure_ascii=False),
        "work_dir": str(work), "render_dir": str(render),
        "result_file": str(pdf), "version_id": v.get("id"),
        "version_code": vcode, "synx_url": url, "priority": 0,
    })
    db.add_event(tid, "published", f"Text PDF {vcode} published as {url or pdf}")

    return {"ok": True, "project": alias, "entity": entity_name, "entity_kind": entity_kind,
            "task_id": tid, "synx_task_id": task_id, "task_name": name,
            "task_type": requested_type, "bot": bot_name,
            "version_code": vcode, "version_id": v.get("id"), "url": url,
            "media_kind": media.get("kind"), "pdf": str(pdf),
            "pdf_info": check, "work_dir": str(work), "render_dir": str(render),
            "attached": [str(a) for a in files_to_attach],
            "publication": publication}
