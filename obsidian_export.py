"""Export a synx project's assets to an Obsidian section: one note per asset,
a map note (MOC) and a JSON Canvas relationship map."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from config import load_config
from synx import SynxClient

DEFAULT_VAULT = Path("/home/jaroslav/obsidian_first_storage")
FOLDERS = {"Location": "Locations", "Model": "Models", "Char": "Characters",
           "script": "Script", "shot": "Shots"}


def _safe(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or "")).strip("_") or "note"


def _yaml(value: str) -> str:
    return json.dumps(str(value or ""), ensure_ascii=False)


def _vault(cfg: dict[str, Any]) -> Path:
    raw = str((cfg.get("paths") or {}).get("obsidian_vault") or "").strip()
    return Path(raw).expanduser() if raw else DEFAULT_VAULT


def export_project(project: str = "test_proekt_vfx", section: str = "TEST_PROJ",
                   vault: str | Path | None = None) -> dict[str, Any]:
    cfg = load_config()
    dest = (Path(vault).expanduser() if vault else _vault(cfg)) / section
    dest.mkdir(parents=True, exist_ok=True)

    client = SynxClient(cfg["synx"]["base_url"], cfg["synx"]["login"], cfg["synx"]["password"])
    proj = next((p for p in client.projects()
                 if str(p.get("alias") or "").lower() == project.lower()
                 or str(p.get("name") or "").lower() == project.lower()), None)
    if not proj:
        raise ValueError(f"project not found: {project}")
    alias = str(proj.get("alias") or project)
    entities = [e for e in client.entities(proj["id"])
                if str(e.get("entity_kind")) != "sequence"]
    by_name = {str(e.get("name")): e for e in entities}
    for e in entities:
        tasks = client.tasks(e["id"])
        e["_tasks"] = tasks
        e["_versions"] = client.versions(e["id"])

    written: list[str] = []
    folders = {v: dest / v for v in set(FOLDERS.values())}
    for f in folders.values():
        f.mkdir(parents=True, exist_ok=True)

    def folder_for(e: dict[str, Any]) -> Path:
        kind = str(e.get("asset_type") or "")
        if kind == "script":
            return folders["Script"]
        if not kind and e.get("entity_kind") == "shot":
            return folders["Shots"]
        return folders.get(FOLDERS.get(kind, ""), dest)

    # one note per asset
    for e in entities:
        name = str(e.get("name"))
        disp = str(e.get("display_name") or name)
        kind = str(e.get("asset_type") or "")
        ekind = str(e.get("entity_kind") or "asset")
        linked = e.get("linked_assets") or []
        tasks = e.get("_tasks") or []
        versions = e.get("_versions") or []
        tag = {"Location": "location", "Model": "object", "Char": "character",
               "script": "script"}.get(kind, "asset")
        if ekind == "shot" and not kind:
            tag = "shot"
        lines = [
            "---",
            f"id: {_yaml(e.get('id'))}",
            f"name: {_yaml(name)}",
            f"display_name: {_yaml(disp)}",
            f"project: {_yaml(alias)}",
            f"asset_type: {_yaml(kind)}",
            f"entity_kind: {_yaml(ekind)}",
            f"status: {_yaml(e.get('status') or 'todo')}",
            f"versions: {len(versions)}",
            f"tasks: {len(tasks)}",
            "tags:",
            f"  - {section}",
            f"  - asset/{tag}",
            "---",
            "",
            f"# {disp}",
            "",
            f"`{name}` · **{kind or ekind}** · статус: {e.get('status') or 'todo'}",
            "",
        ]
        info = str(e.get("info") or "").strip()
        lines += ["## ТЗ", "", info or "_ТЗ не заполнено._", ""]
        lines += ["## Связи", ""]
        if linked:
            for a in linked:
                an = a.get("name") or ""
                at = a.get("asset_type") or ""
                ad = a.get("display_name") or an
                lines.append(f"- [[{an}]] — {at}: {ad}")
        else:
            lines.append("_Связей нет._")
        lines += ["", "## Задачи", ""]
        if tasks:
            for t in tasks:
                lines.append(f"- **{t.get('task_name')}** ({t.get('task_type')}, "
                             f"{t.get('status')}) — `{t.get('id')}`")
        else:
            lines.append("_Задач нет._")
        if versions:
            lines += ["", "## Версии", ""]
            for v in versions:
                vc = v.get("version_code") or ""
                media = (v.get("media") or {}).get("kind") or "file"
                lines.append(f"- **{vc}** — {media}, задача: {v.get('task_name')}")
        note = folder_for(e) / f"{_safe(name)}.md"
        note.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
        written.append(str(note.relative_to(dest)))

    # remove stale markdown that this exporter no longer produces
    produced = {(dest / w).resolve() for w in written}
    for old in dest.rglob("*.md"):
        if old.resolve() not in produced and not old.name.endswith("Карта ассетов.md"):
            try:
                old.unlink()
            except Exception:
                pass

    # map note
    locs = [e for e in entities if e.get("asset_type") == "Location"]
    models = [e for e in entities if e.get("asset_type") == "Model"]
    chars = [e for e in entities if e.get("asset_type") == "Char"]
    maps = [
        f"# {section} — карта ассетов",
        "",
        f"Проект: **{alias}** · сущностей: **{len(entities)}** "
        f"(локаций {len(locs)}, объектов {len(models)}, персонажей {len(chars)}, "
        f"скрипт {len([x for x in entities if x.get('asset_type') == 'script'])}, "
        f"шотов {len([x for x in entities if not x.get('asset_type') and x.get('entity_kind') == 'shot'])}).",
        "",
        "Визуальная карта: [[Карта ассетов.canvas]]",
        "",
        "## Локации",
        "",
    ]
    children: dict[str, list[dict[str, Any]]] = {}
    for e in locs:
        children[str(e.get("name"))] = []
    for e in models + chars:
        for a in e.get("linked_assets") or []:
            if a.get("name") in children:
                children[a["name"]].append(e)
    for e in locs:
        n = str(e.get("name"))
        maps += [f"### [[{n}|{e.get('display_name') or n}]]", ""]
        kids = children.get(n) or []
        if kids:
            for k in kids:
                maps.append(f"- [[{k.get('name')}]] — {k.get('asset_type')}: "
                            f"{k.get('display_name') or k.get('name')}")
        else:
            maps.append("_Объектов не привязано._")
        maps.append("")
    maps += ["## Объекты по типам", ""]
    for e in sorted(models, key=lambda x: str(x.get("name"))):
        maps.append(f"- [[{e.get('name')}]] — {e.get('display_name') or e.get('name')}")
    maps += ["", "## Персонажи", ""]
    for e in sorted(chars, key=lambda x: str(x.get("name"))):
        maps.append(f"- [[{e.get('name')}]] — {e.get('display_name') or e.get('name')}")
    shots = [e for e in entities if not e.get("asset_type") and e.get("entity_kind") == "shot"]
    if shots:
        maps += ["", "## Шоты", ""]
        for e in sorted(shots, key=lambda x: str(x.get("name"))):
            maps.append(f"- [[{e.get('name')}]] — shot")
    other = [e for e in entities
             if e.get("asset_type") not in ("Location", "Model", "Char")
             and not (not e.get("asset_type") and e.get("entity_kind") == "shot")]
    if other:
        maps += ["", "## Прочее", ""]
        for e in other:
            maps.append(f"- [[{e.get('name')}]] — {e.get('asset_type') or 'asset'}")
    (dest / f"{section}.md").write_text("\n".join(maps).rstrip() + "\n", encoding="utf-8")
    written.append(f"{section}.md")

    # canvas map
    nodes = []
    edges = []
    node_w, node_h = 280, 80
    hub_w, hub_h = 320, 100
    cluster_gap_x, cluster_gap_y = 500, 220
    child_cols = 3
    child_dx, child_dy = node_w + 40, node_h + 30
    cluster_w = node_w * child_cols + 40 * (child_cols - 1) + 120
    cluster_h_base = hub_h
    positions: dict[str, tuple[int, int]] = {}
    placed: set[str] = set()
    for li, loc in enumerate(sorted(locs, key=lambda x: str(x.get("name")))):
        col = li % 2
        row = li // 2
        x = col * (cluster_w + cluster_gap_x)
        kids = children.get(str(loc.get("name"))) or []
        rows = max(1, (len(kids) + child_cols - 1) // child_cols)
        y = row * (max(cluster_h_base, rows * child_dy) + cluster_gap_y)
        positions[str(loc.get("name"))] = (x, y)
        nodes.append({"id": str(loc.get("id")), "type": "file",
                      "file": f"{section}/{FOLDERS['Location']}/{_safe(str(loc.get('name')))}.md",
                      "x": x, "y": y, "width": hub_w, "height": hub_h})
        for ki, k in enumerate(kids):
            kid = str(k.get("id"))
            if kid not in placed:
                kc, kr = ki % child_cols, ki // child_cols
                kx = x + hub_w + 80 + kc * child_dx
                ky = y + kr * child_dy
                folder = FOLDERS.get(str(k.get("asset_type")), "")
                nodes.append({"id": kid, "type": "file",
                              "file": f"{section}/{folder}/{_safe(str(k.get('name')))}.md",
                              "x": kx, "y": ky, "width": node_w, "height": node_h})
                placed.add(kid)
            edges.append({"id": f"e_{loc.get('id')}_{k.get('id')}",
                          "fromNode": str(loc.get("id")), "fromSide": "right",
                          "toNode": str(k.get("id")), "toSide": "left"})
    canvas = {"nodes": nodes, "edges": edges}
    (dest / "Карта ассетов.canvas").write_text(
        json.dumps(canvas, ensure_ascii=False, indent=1), encoding="utf-8")
    written.append("Карта ассетов.canvas")

    return {"ok": True, "vault": str(dest), "section": section, "project": alias,
            "entities": len(entities), "locations": len(locs), "objects": len(models),
            "characters": len(chars), "files": len(written), "notes": written}


if __name__ == "__main__":
    import sys
    project = sys.argv[1] if len(sys.argv) > 1 else "test_proekt_vfx"
    section = sys.argv[2] if len(sys.argv) > 2 else "TEST_PROJ"
    print(json.dumps(export_project(project, section), ensure_ascii=False, indent=1))
