from __future__ import annotations

import re
from pathlib import Path
from typing import Any


def _safe(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", str(s or "")).strip("_") or "task"


def shot_dir(cfg: dict[str, Any], project_alias: str, shot: str,
             sequence: str = "", entity_kind: str = "shot") -> Path:
    paths = cfg.get("paths") or {}
    root = Path(paths.get("projects_root") or "~/projects").expanduser()
    layout = (paths.get("layouts") or {}).get(project_alias) or paths.get(
        "default_layout") or "assets/{shot}"
    # A shot with a sequence lives under shots/<sequence>/<shot>, even when
    # the project has a generic assets/{shot} layout for asset entities.
    if entity_kind == "shot" and sequence and "{sequence}" not in layout:
        layout = "shots/{sequence}/{shot}"
    rel = (layout
           .replace("{sequence}", _safe(sequence or "shots"))
           .replace("{shot}", _safe(shot))
           .replace("{project}", _safe(project_alias)))
    return root / _safe(project_alias) / rel


def task_work_dir(cfg: dict[str, Any], project_alias: str, shot: str,
                  sequence: str, task_type: str, bot: str,
                  task_name: str, version: int, entity_kind: str = "shot") -> Path:
    base = shot_dir(cfg, project_alias, shot, sequence, entity_kind)
    return (base / _safe(task_type).lower() / "work" / _safe(bot) /
            f"{_safe(task_name)}_v{int(version):03d}")


def render_dir(cfg: dict[str, Any], project_alias: str, shot: str,
               sequence: str, version: int, entity_kind: str = "shot") -> Path:
    return shot_dir(cfg, project_alias, shot, sequence, entity_kind) / "render" / f"v{int(version):03d}"
