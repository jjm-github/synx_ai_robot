from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from config import DATA_DIR, load_config


def _fence(value: Any, lang: str = "") -> str:
    if value is None:
        return "_не задано_"
    if not isinstance(value, str):
        value = json.dumps(value, ensure_ascii=False, indent=2)
    value = value.strip()
    if not value:
        return "_не задано_"
    return f"```{lang}\n{value}\n```"


def plan_markdown(plan: dict[str, Any], pub: dict[str, Any]) -> str:
    shot = pub.get("shot") or "shot"
    status = "Задача не требуется" if plan.get("needs_task") is False else "Нужна новая задача"
    lines = [
        f"# План задачи · {shot}",
        "",
        f"**Статус:** {status}",
        f"**Проект:** `{pub.get('project_alias') or pub.get('project_id') or ''}`",
        f"**Тред:** {pub.get('thread_kind') or '—'} · версия {pub.get('version_number') or '—'}",
        f"**Автор комментария:** {pub.get('author') or '—'}",
        f"**Токены:** вход {((plan.get('_usage') or {}).get('input') or 0)}, выход {((plan.get('_usage') or {}).get('output') or 0)}",
        "",
        "## Комментарий",
        "",
        (pub.get("body") or "").strip() or "_пусто_",
        "",
    ]
    if plan.get("needs_task") is False:
        lines += [
            "## Вывод",
            "",
            plan.get("summary") or "AI считает, что новая задача не нужна.",
            "",
            "## Пояснение",
            "",
            plan.get("workflow_notes") or "—",
            "",
        ]
        return "\n".join(lines)

    lines += [
        "## Задача",
        "",
        f"- **Название:** `{plan.get('task_name') or '—'}`",
        f"- **Тип:** `{plan.get('task_type') or '—'}`",
        f"- **Бот:** `{plan.get('assignee') or '—'}`",
        f"- **Исходная версия:** `{plan.get('source_version') or '—'}`",
        "",
        "## Суть",
        "",
        plan.get("summary") or "—",
        "",
        "## Prompt",
        "",
        _fence(plan.get("prompt"), "text"),
        "",
        "## Negative",
        "",
        _fence(plan.get("negative"), "text"),
        "",
        "## ComfyUI workflow",
        "",
    ]
    workflow = plan.get("workflow")
    if workflow:
        lines += [_fence(workflow, "json"), ""]
    else:
        lines += ["_Воркфлоу не задан AI._", ""]
    if plan.get("workflow_notes"):
        lines += ["**Заметки:** " + str(plan.get("workflow_notes")), ""]
    refs = plan.get("references") or plan.get("_references_saved") or []
    lines += ["## Референсы", ""]
    if refs:
        lines += ["| Файл | Причина |", "|---|---|"]
        for r in refs:
            if isinstance(r, dict):
                lines.append(f"| `{r.get('name') or r.get('file_id') or r.get('path') or '—'}` | {r.get('reason') or '—'} |")
            else:
                lines.append(f"| `{r}` | — |")
    else:
        lines.append("_Нет вложений._")
    lines += [
        "",
        "## Публикация",
        "",
        plan.get("publish_comment") or "—",
        "",
        "---",
        "",
        "_План сгенерирован Synx AI Robot. JSON-исходник сохранён рядом с .md._",
        "",
    ]
    return "\n".join(lines)


def save_plan_markdown(plan: dict[str, Any], pub: dict[str, Any]) -> tuple[str, str]:
    cfg = load_config()
    plans_dir = Path(str(cfg.get("paths", {}).get("plans_dir") or (DATA_DIR / "plans"))).expanduser()
    plans_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{pub.get('shot') or 'shot'}_{pub.get('comment_id') or pub.get('id') or 'plan'}"
    path = plans_dir / f"{stem}.md"
    md = plan_markdown(plan, pub)
    path.write_text(md, encoding="utf-8")
    vault = str(cfg.get("paths", {}).get("obsidian_vault") or "").strip()
    if vault:
        vdir = Path(vault).expanduser() / "Synx AI Robot"
        try:
            vdir.mkdir(parents=True, exist_ok=True)
            (vdir / f"{stem}.md").write_text(md, encoding="utf-8")
        except Exception:
            pass
    return md, str(path)
