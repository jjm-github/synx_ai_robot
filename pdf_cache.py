from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from config import DATA_DIR

CACHE_DIR = DATA_DIR / "pdf_cache"
_JOBS: dict[str, dict[str, Any]] = {}
_LOCK = threading.Lock()


def _pages(pdf: Path) -> int:
    cp = subprocess.run(["pdfinfo", str(pdf)], capture_output=True, text=True, timeout=30)
    if cp.returncode != 0:
        raise RuntimeError("pdfinfo: " + (cp.stderr or cp.stdout or "failed")[:500])
    m = re.search(r"^Pages:\s+(\d+)", cp.stdout or "", re.M)
    if not m:
        raise RuntimeError("pdfinfo: no page count")
    return int(m.group(1))


def _key(pdf: Path, dpi: int) -> str:
    raw = f"{pdf.resolve()}:{pdf.stat().st_mtime_ns}:{dpi}".encode()
    return hashlib.sha1(raw).hexdigest()[:16]


def _page_path(job: dict[str, Any], page: int) -> Path:
    return Path(job["dir"]) / f"page_{int(page):04d}.png"


def _render_one(pdf: Path, page: int, dpi: int, out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix("")
    cp = subprocess.run(
        ["pdftoppm", "-f", str(int(page)), "-l", str(int(page)), "-r", str(int(dpi)),
         "-png", "-singlefile", str(pdf), str(tmp)],
        capture_output=True, text=True, timeout=300)
    if cp.returncode != 0 or not out.is_file():
        raise RuntimeError(f"pdftoppm page {page}: " + (cp.stderr or "failed")[:400])


def start(pdf_path: str | Path, dpi: int = 200, workers: int = 4) -> dict[str, Any]:
    pdf = Path(str(pdf_path)).expanduser().resolve()
    if not pdf.is_file():
        raise FileNotFoundError(str(pdf))
    total = _pages(pdf)
    key = _key(pdf, dpi)
    job_dir = CACHE_DIR / key
    job_dir.mkdir(parents=True, exist_ok=True)
    existing = [p for p in job_dir.glob("page_*.png")]
    job = {"id": key, "path": str(pdf), "dir": str(job_dir), "dpi": int(dpi),
           "total": total, "done": len(existing), "status": "ready" if len(existing) >= total else "running",
           "error": None, "started_at": time.time(), "pages": []}
    with _LOCK:
        current = _JOBS.get(key)
        if current and current.get("status") == "running":
            return {k: v for k, v in current.items() if k != "dir"}
        _JOBS[key] = job
    if job["status"] == "running":
        threading.Thread(target=_render_job, args=(job, pdf, workers), daemon=True).start()
    return {k: v for k, v in job.items() if k != "dir"}


def _render_job(job: dict[str, Any], pdf: Path, workers: int) -> None:
    missing = [i for i in range(1, int(job["total"]) + 1) if not _page_path(job, i).is_file()]
    try:
        with ThreadPoolExecutor(max_workers=max(1, min(8, int(workers or 4)))) as pool:
            futures = {pool.submit(_render_one, pdf, i, int(job["dpi"]), _page_path(job, i)): i
                       for i in missing}
            for fut in as_completed(futures):
                fut.result()
                with _LOCK:
                    job["done"] = sum(1 for i in range(1, int(job["total"]) + 1)
                                      if _page_path(job, i).is_file())
        job["status"] = "ready" if job["done"] >= job["total"] else "error"
    except Exception as exc:
        job["status"] = "error"
        job["error"] = str(exc)[:1000]


def status(job_id: str) -> dict[str, Any]:
    with _LOCK:
        job = _JOBS.get(job_id)
        if not job:
            raise KeyError(job_id)
        out = {k: v for k, v in job.items() if k != "dir"}
    out["pages"] = [i for i in range(1, int(out["total"]) + 1)
                    if _page_path(job, i).is_file()]
    if out["status"] != "error" and len(out["pages"]) >= out["total"]:
        out["status"] = "ready"
    return out


def page_file(job_id: str, page: int) -> Path:
    job = _JOBS.get(job_id)
    if not job:
        raise KeyError(job_id)
    p = _page_path(job, page)
    if not p.is_file():
        raise FileNotFoundError(str(p))
    return p


def render_all(pdf_path: str | Path, dpi: int = 200, workers: int = 4) -> dict[str, Any]:
    job = start(pdf_path, dpi, workers)
    while True:
        st = status(job["id"])
        if st["status"] in ("ready", "error"):
            return st
        time.sleep(0.4)
