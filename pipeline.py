from __future__ import annotations

import json
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import requests

import comfy_server
import db
from config import load_bots, load_config
from links import task_version_url
from paths import render_dir as make_render_dir
from synx import SynxClient, bot_client
from workflows import build_for_task

requests.packages.urllib3.disable_warnings()


class PipelineError(RuntimeError):
    pass


def _replace(value: Any, mapping: dict[str, Any]) -> Any:
    if isinstance(value, str):
        out = value
        for k, v in mapping.items():
            out = out.replace("{{" + k + "}}", str(v))
        return out
    if isinstance(value, list):
        return [_replace(v, mapping) for v in value]
    if isinstance(value, dict):
        return {k: _replace(v, mapping) for k, v in value.items()}
    return value


class ComfyRunner:
    def __init__(self, cfg: dict[str, Any]):
        self.cfg = cfg
        self.base = str(cfg["comfy"].get("base_url") or "http://127.0.0.1:8199").rstrip("/")
        self.output_dir = Path(str(cfg["comfy"].get("output_dir") or "")).expanduser()
        self.input_dir = Path(str(cfg["comfy"].get("input_dir") or "")).expanduser()

    def run(self, task: dict[str, Any], work_dir: Path) -> Path:
        if not comfy_server.status().get("running"):
            comfy_server.ensure()
        workflow = task.get("workflow_json")
        comfy_image = (task.get("source_json") or {}).get("comfy_image")
        if not workflow:
            path = self.cfg["comfy"].get("workflow_path")
            if path:
                workflow = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
            else:
                workflow = build_for_task(task, self.cfg, comfy_image)
        if isinstance(workflow, str):
            workflow = json.loads(workflow)
        src = task.get("source_json") or {}
        mapping = {
            "PROMPT": task.get("prompt") or "",
            "NEGATIVE": task.get("negative") or "",
            "SEED": src.get("seed") or 20261117,
            "IMAGE": comfy_image or src.get("image") or "",
            "WIDTH": src.get("width") or 2048,
            "HEIGHT": src.get("height") or 864,
            "FRAMES": src.get("frames") or 1,
            "SHOT": task.get("shot") or "",
        }
        workflow = _replace(workflow, mapping)
        r = requests.post(f"{self.base}/prompt", json={"prompt": workflow,
                          "client_id": f"robot-{uuid.uuid4().hex[:8]}"}, timeout=60)
        if r.status_code >= 400:
            raise PipelineError(f"ComfyUI /prompt {r.status_code}: {r.text[:1000]}")
        pid = r.json().get("prompt_id")
        if not pid:
            raise PipelineError("ComfyUI did not return prompt_id")
        deadline = time.time() + int(self.cfg["comfy"].get("timeout_sec", 1800))
        files: list[Path] = []
        while time.time() < deadline:
            time.sleep(2)
            h = requests.get(f"{self.base}/history/{pid}", timeout=30).json()
            item = h.get(pid)
            if not item:
                continue
            st = item.get("status") or {}
            if st.get("status_str") == "error":
                raise PipelineError("ComfyUI error: " + json.dumps(st)[:1200])
            if st.get("completed") or st.get("status_str") == "success":
                for out in (item.get("outputs") or {}).values():
                    for key in ("images", "gifs", "videos"):
                        for f in out.get(key) or []:
                            name = f.get("filename")
                            if not name:
                                continue
                            sub = f.get("subfolder") or ""
                            p = (self.output_dir / sub / name) if self.output_dir else Path(name)
                            if p.is_file():
                                files.append(p)
                            else:
                                q = {"filename": name, "subfolder": sub, "type": f.get("type", "output")}
                                rr = requests.get(f"{self.base}/view", params=q, timeout=300)
                                if rr.status_code == 200:
                                    work_dir.mkdir(parents=True, exist_ok=True)
                                    tmp = work_dir / name
                                    tmp.write_bytes(rr.content)
                                    files.append(tmp)
                break
        if not files:
            raise PipelineError("ComfyUI finished without output files (check comfy.output_dir)")
        work_dir.mkdir(parents=True, exist_ok=True)
        dst = work_dir / files[0].name
        # The /view fallback already writes into work_dir, so copying onto the
        # same path raises SameFileError (broke a whole batch of tasks).
        if files[0].resolve() != dst.resolve():
            shutil.copy2(files[0], dst)
        return dst

    def stage_image(self, task: dict[str, Any], work_dir: Path) -> str | None:
        """Put the source image into ComfyUI/input and return its filename."""
        src = task.get("source_json") or {}
        refs = src.get("references") or []
        candidates = [r.get("path") for r in refs if r.get("path")]
        chosen = None
        if src.get("version_id"):
            cfg = load_config()
            client = SynxClient(cfg["synx"]["base_url"], cfg["synx"]["login"], cfg["synx"]["password"])
            versions = client.versions(src["entity_id"])
            v = next((x for x in versions if x.get("id") == src["version_id"]), None)
            if v:
                tmp = work_dir / ("source" + (".png" if (v.get("media") or {}).get("kind") == "image" else ".mp4"))
                client.download_file(v.get("file_id"), tmp)
                if (v.get("media") or {}).get("kind") == "image":
                    chosen = str(tmp)
                else:
                    frames = int((v.get("media") or {}).get("frames") or 1)
                    mid = frames // 2
                    frame = work_dir / "source_mid.png"
                    subprocess.run([
                        "ffmpeg", "-v", "error", "-y", "-i", str(tmp),
                        "-vf", f"select=eq(n\\,{mid}),setsar=1,scale=2048:-2:flags=lanczos",
                        "-frames:v", "1", str(frame),
                    ], check=True)
                    chosen = str(frame)
        if not chosen:
            chosen = next((p for p in candidates if Path(p).suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}), None)
        if not chosen:
            return None
        self.input_dir.mkdir(parents=True, exist_ok=True)
        name = f"robot_{task.get('id')}_{Path(chosen).name}"
        shutil.copy2(chosen, self.input_dir / name)
        return name


class PipelineManager:
    def __init__(self):
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self.current_id: int | None = None
        self.current_stage: str = "idle"
        self.last_error: str | None = None

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive() and not self._stop.is_set())

    def status(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "current_id": self.current_id,
            "stage": self.current_stage,
            "last_error": self.last_error,
            "queue": db.one("SELECT count(*) AS n FROM tasks WHERE status='queued' AND paused=0 AND skipped=0")["n"],
        }

    def start(self) -> dict[str, Any]:
        with self._lock:
            if self._thread and self._thread.is_alive() and not self._stop.is_set():
                return self.status()
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop, name="pipeline", daemon=True)
            self._thread.start()
        return self.status()

    def stop(self) -> dict[str, Any]:
        self._stop.set()
        return self.status()

    def _loop(self) -> None:
        while not self._stop.is_set():
            task = db.next_task()
            if not task:
                time.sleep(2)
                continue
            self._process(task)
        self.current_id = None
        self.current_stage = "idle"

    def _stage(self, tid: int, stage: str, progress: float, **extra: Any) -> bool:
        row = db.get_task(tid)
        if not row:
            return False
        if row.get("paused") or row.get("skipped"):
            if row.get("paused"):
                db.update_task(tid, status="paused", stage="paused")
            else:
                db.update_task(tid, status="skipped", stage="skipped")
            return False
        if self._stop.is_set():
            db.update_task(tid, status="queued", stage="stopped")
            return False
        self.current_stage = stage
        db.update_task(tid, stage=stage, progress=progress, **extra)
        return True

    def _run_passport(self, tid: int, task: dict[str, Any], plan: dict[str, Any]) -> None:
        """One visible pipeline task = 9 views + preview -> 10-page PDF -> version."""
        import passport_batch
        import chat
        import doc_flow
        import publish_flow
        from pathlib import Path

        src = task.get("source_json") or {}
        name = str(task.get("shot") or "")
        views = int(plan.get("views") or 9)
        paths = []
        for i, (suffix, angle) in enumerate(passport_batch.VIEWS[:views], 1):
            if not self._stage(tid, f"passport view {i}/{views}", 10 + int(80 * i / views)):
                return
            desc = passport_batch.LOCATION_PROMPTS.get(name, name)
            prompt = (f"Concept art of {desc}. {angle}. Animated feature background "
                      "painting, watercolor and gouache, detailed environment, cinematic "
                      "composition, no people, no text, no logos")
            gen = chat.generate_image(
                prompt, passport_batch.NEG, [],
                options={"workflow": "flux3_t2i", "width": 1536, "height": 864,
                         "steps": 26, "cfg": 2.0, "seed": 20261117 + i})
            if not gen:
                raise PipelineError("passport view returned no image")
            paths.append(Path(gen[0]["path"]))

        if not self._stage(tid, "passport assembly", 92):
            return
        work = Path(str(task.get("work_dir") or ""))
        work.mkdir(parents=True, exist_ok=True)
        preview = passport_batch.contact_sheet(
            paths, work / f"{name}_preview.png",
            f"{src.get('display_name') or name} — {len(paths)} views")
        pdf = work / "location_passport.pdf"
        publish_flow.make_pdf([preview, *paths], pdf, page_size="A3", dpi=144, margin_mm=8)
        info = publish_flow.verify_pdf(pdf)
        if not info.get("ok"):
            raise PipelineError("passport pdf: " + "; ".join(info.get("errors") or []))
        res = doc_flow.publish_pdf_as_version(
            str(src.get("project_alias") or ""), name, pdf,
            task_name=str(plan.get("synx_task_name") or "location_develop"),
            task_type=str(plan.get("synx_task_type") or "Concept"),
            bot_login=str(task.get("bot") or "a.smirnov"),
            comment=(f"Паспорт локации: {src.get('display_name') or name} — "
                     f"{len(paths)} видов + превью, {info.get('pages')} стр., A3."),
            record_db=False)
        db.update_task(tid, status="done", stage="published", progress=100,
                       result_file=str(pdf), version_id=res.get("version_id"),
                       version_code=res.get("version_code"), synx_url=res.get("url"),
                       finished_at=db.now(), error=None)
        db.add_event(tid, "published",
                     f"passport {res.get('version_code')}: {res.get('url') or pdf}")

    def _process(self, task: dict[str, Any]) -> None:
        tid = int(task["id"])
        self.current_id = tid
        started = time.time()
        db.update_task(tid, status="running", stage="analysis", progress=5,
                       started_at=db.now(), error=None)
        db.add_event(tid, "start", f"task {task.get('task_name')} started")

        stop_hb = threading.Event()

        def heartbeat():
            while not stop_hb.wait(5):
                db.update_task(tid, elapsed_sec=round(time.time() - started, 1))

        hb = threading.Thread(target=heartbeat, daemon=True)
        hb.start()

        try:
            # analysis stage: plan is already produced by AI at publication analysis time
            plan = task.get("plan_json") or {}
            usage = plan.get("_usage") if isinstance(plan, dict) else None
            if usage:
                db.update_task(tid, tokens_in=int(usage.get("input") or 0),
                               tokens_out=int(usage.get("output") or 0))
            if not self._stage(tid, "analysis", 15):
                return
            time.sleep(0.3)

            if isinstance(plan, dict) and plan.get("passport"):
                self._run_passport(tid, task, plan)
                return

            if not self._stage(tid, "generation", 40):
                return
            cfg = load_config()
            work_root = Path(cfg["pipeline"].get("work_root") or "data/work")
            work = Path(task.get("work_dir") or (work_root / f"task_{tid}"))
            work.mkdir(parents=True, exist_ok=True)
            runner = ComfyRunner(cfg)
            comfy_image = runner.stage_image(task, work)
            if comfy_image:
                task.setdefault("source_json", {})["comfy_image"] = comfy_image
            result = runner.run(task, work)
            db.update_task(tid, result_file=str(result))
            if not self._stage(tid, "assembly", 70):
                return

            # assembly: keep the produced image/video as a render in render/vXXX
            src_meta = task.get("source_json") or {}
            target_ver = int(src_meta.get("target_version") or 1)
            rdir = Path(task.get("render_dir") or make_render_dir(
                cfg, src_meta.get("project_alias") or task.get("project_id") or "project",
                task.get("shot") or "shot", src_meta.get("sequence") or "", target_ver,
                entity_kind=src_meta.get("entity_kind") or "shot"))
            rdir.mkdir(parents=True, exist_ok=True)
            ext = result.suffix.lower()
            final = rdir / f"{task.get('shot') or 'shot'}_{task.get('task_name') or 'task'}_v{target_ver:03d}{ext}"
            shutil.copy2(result, final)
            if ext in {".mp4", ".mov", ".webm", ".mkv"}:
                subprocess.run([
                    "ffmpeg", "-v", "error", "-y", "-ss", "0.5", "-i", str(result),
                    "-frames:v", "1", str(rdir / f"{task.get('shot') or 'shot'}_{task.get('task_name') or 'task'}_v{target_ver:03d}_preview.png"),
                ], check=False)
            db.update_task(tid, result_file=str(final), render_dir=str(rdir))

            if not self._stage(tid, "review", 88):
                return
            # publish
            if cfg["pipeline"].get("auto_publish") and task.get("synx_task_id") and task.get("entity_id"):
                bot = next((b for b in load_bots() if b.get("login") == task.get("bot") and b.get("enabled", True)), None)
                if not bot:
                    raise PipelineError(f"bot not found or disabled: {task.get('bot')}")
                bc = bot_client(cfg["synx"]["base_url"], bot)
                v = bc.publish_version(
                    task["entity_id"], final, task["synx_task_id"],
                    comment=(plan.get("publish_comment") or "robot: новая версия"),
                    minutes=10, note="Synx AI Robot pipeline",
                )
                t2 = dict(task)
                t2["version_code"] = v.get("version_code")
                url = task_version_url(cfg, t2)
                db.update_task(tid, version_id=v.get("id"), version_code=v.get("version_code"), synx_url=url)
            db.update_task(tid, status="done", stage="done", progress=100,
                           elapsed_sec=round(time.time() - started, 1), finished_at=db.now())
            db.add_event(tid, "done", "task finished")
        except Exception as exc:
            self.last_error = str(exc)
            db.update_task(tid, status="error", stage="error", error=str(exc)[:4000],
                           elapsed_sec=round(time.time() - started, 1))
            db.add_event(tid, "error", str(exc)[:4000])
        finally:
            stop_hb.set()
            self.current_id = None


manager = PipelineManager()
