"""Create a synx shot structure from a shot-by-shot script.

The script must contain canonical IDs, e.g.:

    ## S01 · НАТ. ОКЕАН ...
    **S01-01 · WS · 8 c**
    s001_0010 · 200 кадров
    Чёрная вода, дождь...

Rules:
- shot name = the canonical `sNNN_MMMM` ID from the script;
- display_name = the canonical shot name (s001_0010), thesis goes to info;
- info = code · plan, canonical ID · frames, thesis and dialogue;
- cut_out = cut_in + frames - 1 so `length` equals the script duration;
- no tasks and no versions are created;
- shots are linked to the location and to participating characters/props.
"""
from __future__ import annotations

import re
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

from config import load_config
from synx import SynxClient

CHARACTERS = {
    "надя": "char_nadya", "сергей": "char_sergey", "лиза": "char_liza",
    "инна": "char_inna", "корнеев": "char_korneev", "дубров": "char_dubrov",
    "рыбак": "char_fisherman", "петрович": "char_petrovich",
}

OBJECT_KEYWORDS = {
    "obj_balcony": ["балкон"],
    "obj_bank_clock": ["часы банка", "уличные часы", "банк"],
    "obj_beacon_lights": ["маячк", "мигалк"],
    "obj_blanket": ["плед"],
    "obj_bottle_glass": ["бутылк"],
    "obj_boxes": ["коробк"],
    "obj_car_dashboard_clock": ["приборн", "панель авто"],
    "obj_chicken_coop": ["курятник", "кур"],
    "obj_cigarette": ["сигарет", "папирос", "курит"],
    "obj_class_lamps": ["классные лампы", "лампы на потолке"],
    "obj_class_window": ["окно класса", "классное окно"],
    "obj_column": ["колонн", "опор"],
    "obj_column_field": ["колонн", "опор"],
    "obj_column_sea": ["колонн", "опор"],
    "obj_column_city": ["колонн", "опор"],
    "obj_column_sample": ["образец"],
    "obj_commission_papers": ["комисси", "бумаги комиссии"],
    "obj_desks": ["парт"],
    "obj_documents_folder": ["документ", "папк"],
    "obj_drawings": ["чертеж", "схем"],
    "obj_embankment_rail": ["парапет", "набережн"],
    "obj_escalator": ["эскалатор"],
    "obj_facades": ["фасад"],
    "obj_farm_papers": ["бумаги по ферме", "бумаг"],
    "obj_fork_plate": ["вилк", "тарелк"],
    "obj_furrow": ["борозд"],
    "obj_grain_sack": ["мешок", "зерн"],
    "obj_greenhouse": ["теплиц"],
    "obj_hitchhike_car": ["попутк", "машина"],
    "obj_instruments": ["прибор"],
    "obj_kitchen_table": ["кухонный стол", "стол"],
    "obj_lab_screens": ["экран"],
    "obj_metro_entrance": ["метро"],
    "obj_nail_polish": ["лак", "ногт"],
    "obj_notebook_korneev": ["блокнот"],
    "obj_pillars_five": ["пять столбов", "столбы"],
    "obj_placard": ["плакат"],
    "obj_plowed_field": ["вспаханн"],
    "obj_podcast_mic": ["микрофон", "подкаст"],
    "obj_porch": ["крыльц", "ступен"],
    "obj_power_pole_box": ["щиток", "распределительн", "коробка на столбе"],
    "obj_radio": ["радио"],
    "obj_radio_kitchen": ["радио"],
    "obj_rain_rig": ["ливень", "дождевой риг"],
    "obj_rescue_bus": ["автобус", "мчс"],
    "obj_road_barrier": ["огражден", "конус"],
    "obj_rye_field": ["рожь", "поле ржи"],
    "obj_rye_strip": ["полоса ржи"],
    "obj_seed_crate": ["ящик", "рассад"],
    "obj_seiner": ["сейнер"],
    "obj_shadow_grid": ["тень-решётк", "решётк"],
    "obj_shadow_grid_wall": ["стена с решётк", "тень опоры"],
    "obj_shadow_lamps_shop": ["теневые лампы", "витрин"],
    "obj_smartphones_crowd": ["телефон", "смартфон"],
    "obj_sun_picture": ["рисунок солнца", "солнц"],
    "obj_tablet_dubrov": ["планшет"],
    "obj_traffic_lights": ["светофор"],
    "obj_train": ["поезд"],
    "obj_train_window": ["окно вагона", "стекло вагона"],
    "obj_tv_set": ["телевизор", "тв"],
    "obj_water_glass": ["стакан с водой", "вода в стакане", "стакан"],
    "obj_wheelhouse_lights": ["ходовые огни", "рубка"],
    "obj_window_dark": ["тёмное окно", "окно"],
    "obj_window_frame": ["оконная рама", "окно"],
}


def assets_for_text(text: str, loc_name: str | None,
                    entities: dict[str, dict[str, Any]]) -> list[str]:
    """Return asset names participating in a shot (location, characters, props)."""
    low = (text or "").lower()
    out: list[str] = []
    loc = entities.get(loc_name or "")
    if loc:
        out.append(loc_name)
    for stem, name in CHARACTERS.items():
        if stem in low and name in entities and name not in out:
            out.append(name)
    for name, stems in OBJECT_KEYWORDS.items():
        e = entities.get(name)
        if not e or name in out:
            continue
        linked = e.get("linked_asset_ids") or []
        if loc and linked and loc["id"] not in linked:
            continue
        if any(st in low for st in stems):
            out.append(name)
    return out


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
                     create_tasks: bool = True,
                     task_name: str = "conform",
                     task_type: str = "Conform",
                     bot_login: str = "a.smirnov",
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
    projects_root = Path((cfg.get("paths") or {}).get("projects_root") or "/mnt/projects")
    client = _retry(lambda: SynxClient(cfg["synx"]["base_url"], cfg["synx"]["login"],
                                       cfg["synx"]["password"]))
    proj = next((p for p in _retry(client.projects)
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
    created = updated = tasks_created = tasks_assigned = 0
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
                   "display_name": sh["name"], "info": info,
                   "cut_in": cut_in, "cut_out": cut_out}
        existing = entities.get(sh["name"])
        try:
            if existing:
                client.update_entity(existing["id"], {
                    "display_name": sh["name"], "info": info,
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
            wanted = assets_for_text(f"{sh['display_name']}\n{sh['body']}", loc_name, entities)
            ids = [entities[n]["id"] for n in wanted if n in entities]
            if ids:
                try:
                    client.update_entity(shot["id"], {"linked_asset_ids": ids})
                except Exception:
                    pass
        if create_folders:
            try:
                (projects_root / alias / "shots" / seq / sh["name"]).mkdir(
                    parents=True, exist_ok=True)
            except Exception:
                pass
        if create_tasks:
            try:
                current_tasks = client.tasks(shot["id"])
                exists = any(t.get("task_name") == task_name
                             and t.get("task_type") == task_type
                             for t in current_tasks)
                if not exists:
                    tz = (f"ТЗ: {task_type} — {sh['name']}\n\n"
                          f"Секвенция: {seq}\n"
                          f"Длительность: {sh['frames']} кадров "
                          f"({round(int(sh['frames']) / fps, 1)} c, {fps} fps)\n\n"
                          f"{info}\n\n"
                          f"ЗАДАЧА: собрать {task_type.lower()} шота {sh['name']}: "
                          f"сохранить длительность {sh['frames']} кадров, проверить стык "
                          f"с соседними шотами секвенции, не менять тайминг и кадрирование.\n\n"
                          f"СДАЧА: conform-файл в рабочей папке шота; длительность "
                          f"{sh['frames']} кадров.")
                    task = client.create_task(shot["id"], task_name, task_type,
                                              comment=tz, bid=3.0, status="todo")
                    tasks_created += 1
                    try:
                        users = {u.get("short_name"): u for u in _retry(client.assignment_users, task["id"])}
                        user = users.get(bot_login)
                        if user:
                            client.assign(task["id"], [user["id"]])
                            tasks_assigned += 1
                    except Exception:
                        pass
            except Exception as exc:
                errors.append({"shot": sh["name"], "error": "task: " + str(exc)[:160]})
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
        rows = [m for m in made if m["seq"] == code]
        by_name = {m["name"]: m for m in rows}
        shot_rows = [sh for sh in shots if sh["seq"] == code]
        auto_lines = [f"СЕКВЕНЦИЯ {code} · {sq['heading']}",
                      f"ТАЙМИНГ: {sq['timing']} · {total} кадров @{fps}fps",
                      f"ЛОКАЦИЯ: {sq['heading']}", "", "СОДЕРЖАНИЕ:"]
        for sh in shot_rows:
            m = by_name.get(sh["name"], {})
            rng = (f"{m.get('cut_in')}–{m.get('cut_out')}"
                   if m.get("cut_in") is not None else "—")
            auto_lines.append(f"- {sh['name']} · {sh.get('display_name') or sh['name']} "
                              f"· {sh['frames']} кадров ({rng}): {sh.get('body') or ''}")
        auto_lines += ["", f"ЗАДАЧА: Conform — {len(rows)} шотов / {total} кадров."]
        auto_info = "\n".join(auto_lines)
        old_info = (se.get("info") or "").strip()
        info = old_info if len(old_info) > len(auto_info) else auto_info
        try:
            client.update_entity(se["id"], {"display_name": code, "info": info})
            try:
                existing = client.entity_comments(se["id"])
            except Exception:
                existing = []
            if not any((c.get("body") or "").strip() == info for c in existing):
                client.entity_comment(se["id"], info)
        except Exception:
            pass

    stage("Готово")
    return {"ok": not errors, "project": alias, "script": str(path),
            "sequences": len(seqs), "shots": len(shots), "created": created,
            "updated": updated, "tasks_created": tasks_created,
            "tasks_assigned": tasks_assigned, "errors": errors,
            "frames_per_sequence": dict(frames_by_seq),
            "total_frames": sum(frames_by_seq.values()),
            "items": made}


def _retry(fn, *args, attempts: int = 5, **kwargs):
    """Retry transient synx/nginx 502/503 errors with a short backoff."""
    last: Exception | None = None
    for i in range(attempts):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:
            last = exc
            msg = str(exc).lower()
            if "502" not in msg and "503" not in msg and "timed out" not in msg:
                raise
            time.sleep(1.5 * (i + 1))
    raise last  # type: ignore[misc]


def create_tasks_for_entities(project: str, *,
                              task_name: str = "conform",
                              task_type: str = "Conform",
                              bot_login: str = "a.smirnov",
                              progress=None) -> dict[str, Any]:
    """Create one missing task per shot in a project (bulk, idempotent)."""
    cfg = load_config()
    fps = int(cfg.get("fps") or 25)
    client = SynxClient(cfg["synx"]["base_url"], cfg["synx"]["login"], cfg["synx"]["password"])
    proj = next((p for p in client.projects()
                 if str(p.get("alias") or "").lower() == project.lower()
                 or str(p.get("name") or "").lower() == project.lower()
                 or str(p.get("id")) == str(project)), None)
    if not proj:
        raise ValueError(f"project not found: {project}")
    shots = sorted((e for e in _retry(client.entities, proj["id"])
                    if e.get("entity_kind") == "shot"), key=lambda e: e.get("name") or "")
    created = assigned = 0
    errors: list[dict[str, str]] = []
    for i, shot in enumerate(shots, 1):
        name = str(shot.get("name") or "")
        try:
            tasks = _retry(client.tasks, shot["id"])
            existing = next((t for t in tasks
                             if t.get("task_name") == task_name
                             and t.get("task_type") == task_type), None)
            if existing:
                if bot_login not in (existing.get("assigned") or []):
                    try:
                        users = {u.get("short_name"): u for u in
                                 _retry(client.assignment_users, existing["id"])}
                        user = users.get(bot_login)
                        if user:
                            _retry(client.assign, existing["id"], [user["id"]])
                            assigned += 1
                    except Exception:
                        pass
                continue
            frames = int(shot.get("length") or 0)
            seconds = round(frames / fps, 1) if frames else 0
            info = (shot.get("info") or "").strip()
            seq = shot.get("sequence_name") or ""
            tz = (f"ТЗ: {task_type} — {name}\n\n"
                  f"Секвенция: {seq or '—'}\n"
                  f"Длительность: {frames} кадров ({seconds} c, {fps} fps)\n\n"
                  f"{info}\n\n"
                  f"ЗАДАЧА: собрать {task_type.lower()} шота {name}: сохранить "
                  f"длительность {frames} кадров, проверить стык с соседними шотами "
                  f"секвенции, не менять тайминг и кадрирование.\n\n"
                  f"СДАЧА: conform-файл в рабочей папке шота; длительность {frames} кадров.")
            task = _retry(client.create_task, shot["id"], task_name, task_type,
                          comment=tz, bid=3.0, status="todo")
            created += 1
            try:
                users = {u.get("short_name"): u for u in client.assignment_users(task["id"])}
                user = users.get(bot_login)
                if user:
                    client.assign(task["id"], [user["id"]])
                    assigned += 1
            except Exception:
                pass
        except Exception as exc:
            errors.append({"shot": name, "error": str(exc)[:200]})
        if progress and i % 10 == 0:
            progress({"type": "stage", "text": f"Задачи: {i}/{len(shots)}"})
    return {"ok": True, "shots": len(shots), "created": created,
            "assigned": assigned, "errors": errors}
