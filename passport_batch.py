"""Batch location passports: 9 views + 1 preview page (10-page PDF) per location.

Each view is a separate FLUX generation with a different camera/light task;
page 1 is a contact sheet with all 9 views, pages 2-10 are the full views.
The finished PDF is published as a new version under the location's
`location_develop` (Concept) task.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent
STATUS_PATH = ROOT / "data" / "passport_batch.json"
LOG_PATH = ROOT / "data" / "logs" / "passport_batch.log"

VIEWS = [
    ("01_establishing", "wide establishing shot, full location layout, golden hour"),
    ("02_reverse", "reverse angle from the opposite side, same location, overcast light"),
    ("03_low_angle", "low camera angle, foreground detail, dramatic perspective"),
    ("04_aerial", "high aerial view, top-down plan of the location, clear weather"),
    ("05_detail", "close-up material detail, texture and wear, shallow depth of field"),
    ("06_interior", "interior corner / inside view of the main structure, practical light"),
    ("07_approach", "approach path and entrance, view from the road, morning haze"),
    ("08_light", "light study, late evening, long shadows, warm rim light"),
    ("09_mood", "atmospheric mood shot, fog and silence, cinematic composition"),
]

NEG = ("text, watermark, logo, caption, people, characters, old wooden barn, ruined shed, "
       "vintage tractor, abandoned buildings")

# Short English descriptors: the display name alone is often ambiguous for FLUX
# ("Flat - night" produced villages and forests instead of an apartment).
LOCATION_PROMPTS = {
    "loc_ferma_gordeevy": "small family farm in the Moscow region, rye field, greenhouse, log house, early morning",
    "loc_ferma_late_summer": "modern agro-industrial farm, silo towers, rapeseed fields, late summer",
    "loc_ferma_strip_under": "narrow strip of field under a giant concrete column, sunset, long shadows",
    "loc_flat_night": "interior of a small Moscow apartment at night, window, warm lamp, farm papers on the table",
    "loc_house_kitchen": "interior of a village house kitchen, old TV, table, radio, daylight",
    "loc_inna_flat": "interior of a small Moscow apartment, moving boxes, balcony, rainy day",
    "loc_institute_lab": "scientific laboratory interior, instruments, screens, blueprints, cold light",
    "loc_msk_embankment": "Moscow embankment, five giant concrete columns leaning inward, spring",
    "loc_msk_morning": "Moscow morning street under giant columns, people going to work, long shadow",
    "loc_msk_rituals": "Moscow city rituals: prayer at a column base, picket, shadow lamps shop",
    "loc_msk_street_shadow": "Moscow street at 15:40, sudden shadow, stopped escalator, traffic lights",
    "loc_ocean_seiner": "fishing seiner on a dark ocean at night, rain, giant column rising from the water",
    "loc_school_class": "school classroom interior, lamps on during the day, window with a column outside",
    "loc_village_road": "village road evacuation, buses, emergency lights, evening",
}


def _log(msg: str) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def _status(data: dict[str, Any]) -> None:
    STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATUS_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for path in ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                 "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            continue
    return ImageFont.load_default()


def contact_sheet(paths: list[Path], out: Path, title: str) -> Path:
    cols, rows = 3, 3
    tw, th = 640, 360
    pad, header = 18, 90
    W = cols * tw + (cols + 1) * pad
    H = header + rows * th + (rows + 1) * pad
    page = Image.new("RGB", (W, H), "#ffffff")
    draw = ImageDraw.Draw(page)
    draw.text((pad, 22), title, fill="#111111", font=_font(34))
    draw.text((pad, 62), "9 views + preview", fill="#777777", font=_font(18))
    for i, p in enumerate(paths):
        r, c = divmod(i, cols)
        x = pad + c * (tw + pad)
        y = header + pad + r * (th + pad)
        try:
            with Image.open(p) as im:
                im = im.convert("RGB")
                im.thumbnail((tw, th), Image.LANCZOS)
                page.paste(im, (x + (tw - im.width) // 2, y + (th - im.height) // 2))
        except Exception:
            draw.rectangle([x, y, x + tw, y + th], fill="#eeeeee")
        draw.text((x + 6, y + 6), f"{i + 1}", fill="#ffffff", font=_font(30),
                  stroke_width=3, stroke_fill="#000000")
    out.parent.mkdir(parents=True, exist_ok=True)
    page.save(out)
    return out


def run(project: str, locations: list[str], views: int = 9,
        bot: str = "a.smirnov", page_size: str = "A3") -> dict[str, Any]:
    import chat
    import doc_flow
    import publish_flow
    from config import load_config
    from synx import SynxClient

    cfg = load_config()
    client = SynxClient(cfg["synx"]["base_url"], cfg["synx"]["login"], cfg["synx"]["password"])
    proj = next((p for p in client.projects()
                 if str(p.get("alias") or "").lower() == project.lower()), None)
    if not proj:
        raise ValueError(f"project not found: {project}")
    all_locs = [e for e in client.entities(proj["id"]) if e.get("asset_type") == "Location"]
    if locations and [x.lower() for x in locations] != ["all"]:
        wanted = {x.lower() for x in locations}
        all_locs = [e for e in all_locs if str(e.get("name")).lower() in wanted]
    total = len(all_locs)
    _log(f"batch start: {total} locations, {views} views each")

    report: dict[str, Any] = {"started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                              "total": total, "done": 0, "items": [], "errors": []}
    _status(report)
    for idx, e in enumerate(all_locs, 1):
        name = e["name"]
        disp = e.get("display_name") or name
        _log(f"[{idx}/{total}] {name}")
        paths: list[Path] = []
        try:
            for vi, (suffix, angle) in enumerate(VIEWS[:views], 1):
                desc = LOCATION_PROMPTS.get(name, disp)
                prompt = (f"Concept art of {desc}. {angle}. Animated feature background "
                          "painting, watercolor and gouache, detailed environment, "
                          "cinematic composition, no people, no text, no logos")
                _log(f"  view {vi}/{views}: {suffix}")
                gen = chat.generate_image(
                    prompt, NEG, [],
                    options={"workflow": "flux3_t2i", "width": 1536, "height": 864,
                             "steps": 26, "cfg": 2.0, "seed": 20261117 + vi})
                if not gen:
                    raise RuntimeError("no image returned")
                paths.append(Path(gen[0]["path"]))
            preview = contact_sheet(
                paths, ROOT / "data" / "chat" / "pdf" / f"{name}_preview.png",
                f"{disp} — {len(paths)} views")
            from paths import task_work_dir
            versions = client.versions(e["id"])
            next_ver = max([int(v.get("version_number") or 0) for v in versions] + [0]) + 1
            work = task_work_dir(cfg, project, name, "", "Concept", bot,
                                 "location_develop", next_ver, entity_kind="asset")
            work.mkdir(parents=True, exist_ok=True)
            pdf = work / f"location_develop_v{next_ver:03d}.pdf"
            publish_flow.make_pdf([preview, *paths], pdf, page_size=page_size,
                                  dpi=144, margin_mm=8)
            info = publish_flow.verify_pdf(pdf)
            if not info.get("ok"):
                raise RuntimeError("pdf check: " + "; ".join(info.get("errors") or []))
            res = doc_flow.publish_pdf_as_version(
                project, name, pdf, task_name="location_develop", task_type="Concept",
                bot_login=bot,
                comment=(f"Паспорт локации: {disp} — {len(paths)} видов + превью, "
                         f"{info.get('pages')} стр., A3."))
            report["items"].append({"location": name, "version": res.get("version_code"),
                                    "url": res.get("url"), "pages": info.get("pages"),
                                    "views": len(paths)})
            report["done"] = idx
            _status(report)
            _log(f"  published {res.get('version_code')} ({info.get('pages')} pages)")
        except Exception as exc:
            _log(f"  ERROR {name}: {exc}")
            report["errors"].append({"location": name, "error": str(exc)[:500]})
            _status(report)

    report["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    _status(report)
    _log("batch finished")
    return report


def start(project: str, locations: list[str] | None, views: int = 9,
          bot: str = "a.smirnov") -> dict[str, Any]:
    """Spawn the batch in a detached process and return its status info."""
    running = is_running()
    if running:
        return {**running, "already_running": True}
    args = [sys.executable, str(ROOT / "passport_batch.py"),
            "--project", project, "--views", str(views), "--bot", bot]
    locs = locations or ["all"]
    args += ["--locations", ",".join(locs)]
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    log = (ROOT / "data" / "logs" / "passport_batch.out").open("ab")
    proc = subprocess.Popen(args, cwd=str(ROOT), stdin=subprocess.DEVNULL,
                            stdout=log, stderr=subprocess.STDOUT,
                            start_new_session=True)
    status = {"pid": proc.pid, "project": project, "views": views,
              "locations": locs, "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
              "status": "running", "status_file": str(STATUS_PATH),
              "log": str(LOG_PATH)}
    _status(status)
    return status


def is_running() -> dict[str, Any] | None:
    """Return the running batch status, if any."""
    try:
        st = json.loads(STATUS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return None
    pid = int(st.get("pid") or 0)
    if not pid:
        return None
    try:
        os.kill(pid, 0)
        return st
    except Exception:
        return None


def start_via_pipeline(project: str, locations: list[str] | None = None,
                       views: int = 9, bot: str = "a.smirnov",
                       start_manager: bool = True) -> dict[str, Any]:
    """Queue one visible pipeline task per location (9 views + preview)."""
    import db
    from config import load_config
    from paths import task_work_dir, render_dir
    from synx import SynxClient
    import pipeline

    cfg = load_config()
    client = SynxClient(cfg["synx"]["base_url"], cfg["synx"]["login"], cfg["synx"]["password"])
    proj = next((p for p in client.projects()
                 if str(p.get("alias") or "").lower() == project.lower()), None)
    if not proj:
        raise ValueError(f"project not found: {project}")
    all_locs = [e for e in client.entities(proj["id"]) if e.get("asset_type") == "Location"]
    if locations and [x.lower() for x in locations] != ["all"]:
        wanted = {x.lower() for x in locations}
        all_locs = [e for e in all_locs if str(e.get("name")).lower() in wanted]
    if not all_locs:
        raise ValueError("no locations to process")
    running = is_running()
    if running:
        return {"already_running": True, **running}

    ids = []
    for e in all_locs:
        name = e["name"]
        task = next((t for t in client.tasks(e["id"])
                     if t.get("task_name") == "location_develop"
                     and t.get("task_type") == "Concept"), None)
        versions = client.versions(e["id"])
        next_ver = max([int(v.get("version_number") or 0) for v in versions] + [0]) + 1
        work = task_work_dir(cfg, project, name, "", "Concept", bot,
                             "location_develop", next_ver, entity_kind="asset")
        render = render_dir(cfg, project, name, "", next_ver, entity_kind="asset")
        plan = {"passport": True, "views": int(views),
                "synx_task_name": "location_develop", "synx_task_type": "Concept",
                "summary": f"Location passport: {e.get('display_name')}",
                "workflow_config": {"profile": "flux3_t2i", "steps": 26, "cfg": 2.0,
                                    "width": 1536, "height": 864}}
        src = {"project_id": proj["id"], "project_alias": project,
               "entity_id": e["id"], "entity_kind": "asset", "shot": name,
               "display_name": e.get("display_name") or name,
               "target_version": next_ver}
        tid = db.create_task({
            "publication_id": None, "synx_task_id": task.get("id") if task else None,
            "project_id": proj["id"], "entity_id": e["id"], "shot": name,
            "task_name": "location_passport", "task_type": "Passport", "bot": bot,
            "status": "queued", "stage": "queued", "progress": 0,
            "prompt": f"passport: {views} views + preview",
            "negative": NEG, "plan_json": json.dumps(plan, ensure_ascii=False),
            "plan_md": "", "plan_md_path": "", "workflow_json": None,
            "source_json": json.dumps(src, ensure_ascii=False),
            "work_dir": str(work), "render_dir": str(render), "result_file": None,
            "version_id": None, "version_code": None, "synx_url": None,
            "error": None, "paused": 0, "skipped": 0, "priority": 0,
        })
        ids.append(tid)

    status = pipeline.manager.start() if start_manager else pipeline.manager.status()
    return {"queued": len(ids), "task_ids": ids, "pipeline": status,
            "status_file": str(STATUS_PATH)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", required=True)
    ap.add_argument("--locations", default="all")
    ap.add_argument("--views", type=int, default=9)
    ap.add_argument("--bot", default="a.smirnov")
    args = ap.parse_args()
    locs = [x.strip() for x in args.locations.split(",") if x.strip()]
    run(args.project, locs, args.views, args.bot)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
