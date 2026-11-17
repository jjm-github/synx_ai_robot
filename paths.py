from __future__ import annotations

import re
from pathlib import Path
from typing import Any


def _safe(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", str(s or "")).strip("_") or "task"


def shot_dir(cfg: dict[str, Any], project_alias: str, shot: str,
             sequence: str = "") -> Path:
    paths = cfg.get("paths") or {}
    root = Path(paths.get("projects_root") or "~/projects").expanduser()
    layout = (paths.get("layouts") or {}).get(project_alias) or paths.get(
        "default_layout") or "assets/{shot}"
    rel = (layout
           .replace("{sequence}", _safe(sequence or "shots"))
           .replace("{shot}", _safe(shot))
           .replace("{project}", _safe(project_alias)))
    return root / _safe(project_alias) / rel


def task_work_dir(cfg: dict[str, Any], project_alias: str, shot: str,
                  sequence: str, task_type: str, bot: str,
                  task_name: str, version: int) -> Path:
    base = shot_dir(cfg, project_alias, shot, sequence)
    return (base / _safe(task_type).lower() / "work" / _safe(bot) /
            f"{_safe(task_name)}_v{int(version):03d}")


def render_dir(cfg: dict[str, Any], project_alias: str, shot: str,
               sequence: str, version: int) -> Path:
    return shot_dir(cfg, project_alias, shot, sequence) / "render" / f"v{int(version):03d}"
