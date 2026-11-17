from __future__ import annotations

from urllib.parse import quote
from typing import Any


def _q(s: str) -> str:
    return quote(str(s or ""), safe="-_.~")


def version_url(base_url: str, project_alias: str, kind: str, name: str,
                version_code: str, task_type: str | None = None,
                task_name: str | None = None) -> str:
    base = base_url.rstrip("/") + f"/p/{_q(project_alias)}"
    ent = f"{kind}-{name}"
    ver = f"{kind}-{name}-{version_code}"
    if task_type and task_name:
        task = f"{kind}-{name}-{task_type}-{task_name}"
        return f"{base}?tab=tasks&t={_q(task)}&v={_q(ver)}"
    return f"{base}?s={_q(ent)}&v={_q(ver)}"


def task_version_url(cfg: dict[str, Any], task: dict[str, Any]) -> str | None:
    src = task.get("source_json") or {}
    if isinstance(src, str):
        import json
        try:
            src = json.loads(src)
        except Exception:
            src = {}
    code = task.get("version_code")
    if not code:
        return task.get("synx_url")
    alias = src.get("project_alias") or task.get("project_id")
    kind = src.get("entity_kind") or "shot"
    name = src.get("entity_name") or task.get("shot")
    if not alias or not name:
        return None
    return version_url(cfg["synx"]["base_url"], alias, kind, name, code,
                       task.get("task_type"), task.get("task_name"))
