"""Create a synx shot structure from a shot-by-shot script.

The script must contain canonical IDs, e.g.:

    ## S01 · НАТ. ОКЕАН ...
    **S01-01 · WS · 8 c**
    s001_0010 · 200 кадров
    Чёрная вода, дождь...

Rules:
- shot name = the canonical `sNNN_MMMM` ID from the script;
- display_name = the shot thesis (never the S01-04 code);
- info = code · plan, canonical ID · frames, thesis and dialogue;
- cut_out = cut_in + frames - 1 so `length` equals the script duration;
- no tasks and no versions are created;
- shots are linked to their location asset when a mapping is known.
"""
from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path
from typing import Any

from config import load_config
from synx import SynxClient

PILOT_LOCATION_MAP = {
    "s001": "loc_ocean_seiner", "s002": "loc_ferma_gordeevy",
    "s003": "loc_house_kitchen", "s004": "loc_village_road",
    "s005": "loc_inna_flat", "s006": "loc_institute_lab",
    "s007": "loc_msk_embankment", "s008": "loc_msk_street_shadow",
    "s009": "loc_msk_rituals", "s010": "loc_ferma_late_summer",
    "s011": "loc_ferma_strip_under", "s012": "loc_school_class",
    "s013": "loc_flat_night", "s014": "loc_msk_morning",
}


def parse_script(text: str, fps: int = 25) -> dict[str, Any]:
    lines = (text or "").replace("\r\n", "\n").split("\n")
    seqs: list[dict[str, Any]] = []
    shots: list[dict[str, Any]] = []
    cur = None
    i = 0
    while i < len(lines):
        s = lines[i].strip()
        m = re.match(r"^## S(\d{2})\s*[·:.\-—]\s*(.+)$", s)
        if m:
            cur = f"s{int(m.group(1)):03d}"
            timing = ""
            if i + 1 < len(lines):
                tm = re.search(r"Хронометраж секвенции:\s*(.+)", lines[i + 1])
                if tm:
                    timing = tm.group(1).strip()
            seqs.append({"code": cur, "heading": m.group(2).strip(), "timing": timing})
            i += 1
            continue
        m = re.match(r"^\*\*S(\d{2})-(\d{2})\s*[·:.\-—]\s*([^*]+)\*\*$", s)
        if m:
            block: list[str] = []
            j = i + 1
            while j < len(lines):
                t = lines[j].strip()
                if re.match(r"^## ", t) or re.match(r"^\*\*S\d{2}-\d{2}", t):
                    break
                block.append(lines[j])
                j += 1
            name = None
            frames = None
            body: list[str] = []
            for b in block:
                mm = re.match(r"^\s*(s\d{3}_\d{4})\s*·\s*(\d+)\s*кадров", b)
                if mm and not name:
                    name = mm.group(1)
                    frames = int(mm.group(2))
                    continue
                body.append(b)
            first = next((x.strip() for x in body if x.strip()), "")
            disp = re.sub(r"\*\*|__", "", first)
            disp = re.sub(r"\s+", " ", disp).strip().strip('"«»')
            if len(disp) > 110:
                disp = disp[:107].rstrip() + "…"
            shots.append({
                "seq": cur, "code": f"S{m.group(1)}-{m.group(2)}",
                "plan": m.group(3).strip(), "name": name, "frames": frames,
                "display_name": disp, "body": "\n".join(body).strip(),
            })
            i = j
            continue
        i += 1
    return {"sequences": seqs, "shots": shots, "fps": fps}


def create_structure(project: str, script_path: str | Path, *,
                     fps: int = 25,
                     location_map: dict[str, str] | None = None,
                     link_locations: bool = True,
                     create_folders: bool = True,
                     progress=None) -> dict[str, Any]:
    def stage(msg: str) -> None:
        if progress:
            progress({"type": "stage", "text": msg})

    path = Path(str(script_path)).expanduser()
    if not path.is_file():
        raise FileNotFoundError(f"script not found: {path}")
    parsed = parse_script(path.read_text(encoding="utf-8"), fps=fps)
    seqs = parsed["sequences"]
    shots = parsed["shots"]
    if not shots:
        raise ValueError("в сценарии не найдены шоты вида **S01-01 · WS · 8 c** / s001_0010 · 200 кадров")

    cfg = load_config()
    client = SynxClient(cfg["synx"]["base_url"], cfg["synx"]["login"], cfg["synx"]["password"])
    proj = next((p for p in client.projects()
                 if str(p.get("alias") or "").lower() == project.lower()
                 or str(p.get("name") or "").lower() == project.lower()), None)
    if not proj:
        raise ValueError(f"project not found: {project}")
    alias = str(proj.get("alias") or project)
    entities = {str(e.get("name")): e for e in client.entities(proj["id"])}
    loc_map = dict(PILOT_LOCATION_MAP)
    loc_map.update(location_map or {})

    stage("Создаю шоты по сценарию...")
    frames_by_seq: dict[str, int] = defaultdict(int)
    cut: dict[str, int] = defaultdict(int)
    created = updated = 0
    errors: list[dict[str, str]] = []
    made: list[dict[str, Any]] = []
    for sh in shots:
        seq = str(sh["seq"])
        if not sh["name"] or not sh["frames"]:
            errors.append({"shot": sh.get("code") or "?", "error": "no canonical ID/frames"})
            continue
        cut_in = cut[seq]
        cut_out = cut_in + int(sh["frames"]) - 1
        cut[seq] = cut_out + 1
        frames_by_seq[seq] += int(sh["frames"])
        info = (f"{sh['code']} · {sh['plan']}\n{sh['name']} · {sh['frames']} кадров\n\n"
                f"{sh['body']}").strip()
        payload = {"entity_kind": "shot", "name": sh["name"],
                   "display_name": sh["display_name"] or sh["name"], "info": info,
                   "cut_in": cut_in, "cut_out": cut_out}
        existing = entities.get(sh["name"])
        try:
            if existing:
                client.update_entity(existing["id"], {
                    "display_name": payload["display_name"], "info": info,
                    "cut_in": cut_in, "cut_out": cut_out})
                shot = existing
                updated += 1
            else:
                shot = client.create_entity(proj["id"], payload)
                entities[sh["name"]] = shot
                created += 1
        except Exception as exc:
            errors.append({"shot": sh["name"], "error": str(exc)[:200]})
            continue
        if link_locations:
            loc_name = loc_map.get(seq)
            loc = entities.get(loc_name) if loc_name else None
            if loc and not (shot.get("linked_asset_ids") or []):
                try:
                    client.update_entity(shot["id"], {"linked_asset_ids": [loc["id"]]})
                except Exception:
                    pass
        if create_folders:
            try:
                (Path("/mnt/projects") / alias / "shots" / seq / sh["name"]).mkdir(
                    parents=True, exist_ok=True)
            except Exception:
                pass
        made.append({"name": sh["name"], "seq": seq, "frames": sh["frames"],
                     "cut_in": cut_in, "cut_out": cut_out})

    # sequence metadata (sequences are auto-created by the shots)
    stage("Заполняю секвенции...")
    fresh = {str(e.get("name")): e for e in client.entities(proj["id"])}
    for sq in seqs:
        code = sq["code"]
        se = fresh.get(code)
        if not se or se.get("entity_kind") != "sequence":
            continue
        total = frames_by_seq.get(code, 0)
        info = (f"Секвенция {code} · {sq['heading']}\n{sq['timing']}\n"
                f"Шотов: {sum(1 for m in made if m['seq'] == code)} · Кадров: {total}")
        try:
            client.update_entity(se["id"], {"display_name": sq["heading"], "info": info})
        except Exception:
            pass

    stage("Готово")
    return {"ok": not errors, "project": alias, "script": str(path),
            "sequences": len(seqs), "shots": len(shots), "created": created,
            "updated": updated, "errors": errors,
            "frames_per_sequence": dict(frames_by_seq),
            "total_frames": sum(frames_by_seq.values()),
            "items": made}
