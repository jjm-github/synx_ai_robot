from __future__ import annotations

import json
import mimetypes
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable

import requests

requests.packages.urllib3.disable_warnings()

BOT_LIKE_AUTHORS = {"p.robot", "review-service", "client-review"}


class SynxError(RuntimeError):
    pass


class SynxClient:
    def __init__(self, base_url: str, login: str | None = None, password: str | None = None):
        self.base = base_url.rstrip("/")
        self.s = requests.Session()
        self.s.verify = False
        self.s.headers.update({"Accept": "application/json"})
        if login and password:
            self.login(login, password)

    def login(self, login: str, password: str) -> dict[str, Any]:
        r = self.s.post(f"{self.base}/api/auth/login",
                        json={"short_name": login, "password": password}, timeout=30)
        if r.status_code >= 400:
            raise SynxError(f"synx login {login}: {r.status_code} {r.text[:300]}")
        return r.json()

    def _get(self, path: str, timeout: int = 60) -> Any:
        r = self.s.get(f"{self.base}{path}", timeout=timeout)
        if r.status_code >= 400:
            raise SynxError(f"GET {path}: {r.status_code} {r.text[:300]}")
        return r.json()

    def projects(self) -> list[dict[str, Any]]:
        return self._get("/api/projects")["projects"]

    def entities(self, project_id: str) -> list[dict[str, Any]]:
        return self._get(f"/api/projects/{project_id}/entities")["entities"]

    def tasks(self, entity_id: str) -> list[dict[str, Any]]:
        return self._get(f"/api/entities/{entity_id}/tasks")["tasks"]

    def versions(self, entity_id: str) -> list[dict[str, Any]]:
        return self._get(f"/api/entities/{entity_id}/versions")["versions"]

    def project_versions(self, project_id: str) -> list[dict[str, Any]]:
        return self._get(f"/api/projects/{project_id}/versions")["versions"]

    def project_tasks(self, project_id: str) -> list[dict[str, Any]]:
        return self._get(f"/api/projects/{project_id}/tasks")["tasks"]

    def task_comments(self, task_id: str) -> list[dict[str, Any]]:
        return self._get(f"/api/tasks/{task_id}/comments")["comments"]

    def version_comments(self, version_id: str) -> list[dict[str, Any]]:
        return self._get(f"/api/versions/{version_id}/comments")["comments"]

    def task_types(self, project_id: str) -> list[str]:
        try:
            return self._get(f"/api/projects/{project_id}/task_types")["task_types"]
        except Exception:
            return []

    def download_file(self, file_id: str, dst: Path) -> Path:
        dst.parent.mkdir(parents=True, exist_ok=True)
        r = self.s.get(f"{self.base}/files/{file_id}", timeout=300)
        if r.status_code >= 400:
            raise SynxError(f"download {file_id}: {r.status_code}")
        dst.write_bytes(r.content)
        return dst

    def create_task(self, entity_id: str, task_name: str, task_type: str,
                    comment: str | None = None, bid: float = 3.0,
                    status: str = "wip") -> dict[str, Any]:
        r = self.s.post(
            f"{self.base}/api/entities/{entity_id}/tasks",
            json={"task_name": task_name, "task_type": task_type, "bid": bid,
                  "status": status, "comment": comment}, timeout=60,
        )
        if r.status_code >= 400:
            raise SynxError(f"create task: {r.status_code} {r.text[:500]}")
        return r.json()

    def assign(self, task_id: str, user_ids: list[str]) -> None:
        r = self.s.post(f"{self.base}/api/tasks/{task_id}/assignments",
                        json={"user_ids": user_ids}, timeout=30)
        if r.status_code >= 400:
            raise SynxError(f"assign {task_id}: {r.status_code} {r.text[:300]}")

    def users(self) -> list[dict[str, Any]]:
        # admin-only on production; prefer assignment_users(task_id) for p.robot
        return self._get("/api/users")["users"]

    def assignment_users(self, task_id: str) -> list[dict[str, Any]]:
        return self._get(f"/api/tasks/{task_id}/assignments-form")["users"]

    def task_status(self, task_id: str, status: str) -> None:
        r = self.s.patch(f"{self.base}/api/tasks/{task_id}/status",
                         json={"status": status}, timeout=30)
        if r.status_code >= 400:
            raise SynxError(f"task status {task_id}: {r.status_code} {r.text[:300]}")

    def task_comment(self, task_id: str, body: str) -> dict[str, Any]:
        r = self.s.post(f"{self.base}/api/tasks/{task_id}/comments",
                        data={"body": body}, timeout=30)
        if r.status_code >= 400:
            raise SynxError(f"task comment {task_id}: {r.status_code} {r.text[:300]}")
        return r.json()

    def publish_version(self, entity_id: str, file_path: str | Path, task_id: str,
                        comment: str = "", minutes: int = 1, note: str = "robot") -> dict[str, Any]:
        path = Path(file_path)
        if not path.is_file():
            raise SynxError(f"no file: {path}")
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        with path.open("rb") as fh:
            r = self.s.post(
                f"{self.base}/api/entities/{entity_id}/versions",
                data={"task_id": task_id, "comment": comment,
                      "minutes": str(int(minutes)), "note": note},
                files={"file": (path.name, fh, ctype)}, timeout=600,
            )
        if r.status_code >= 400:
            raise SynxError(f"publish {entity_id}: {r.status_code} {r.text[:500]}")
        return r.json()

    # --- scan ---------------------------------------------------------------
    def _worker_session(self) -> requests.Session:
        s = requests.Session()
        s.verify = False
        s.headers.update({"Accept": "application/json"})
        s.cookies.update(self.s.cookies.get_dict())
        return s

    def scan_project(self, project_alias: str,
                     progress: Callable[[str], None] | None = None,
                     include_task_comments: bool = True,
                     workers: int = 12) -> list[dict[str, Any]]:
        projects = self.projects()
        project = next((p for p in projects if p.get("alias") == project_alias or p.get("id") == project_alias), None)
        if not project:
            raise SynxError(f"project not found: {project_alias}")
        entities = self.entities(project["id"])
        if progress:
            progress(f"entities: {len(entities)}")
        records: list[dict[str, Any]] = []
        lock = threading.Lock()

        def fetch_entity(ent: dict[str, Any]) -> list[dict[str, Any]]:
            s = self._worker_session()
            out: list[dict[str, Any]] = []
            try:
                versions = s.get(f"{self.base}/api/entities/{ent['id']}/versions", timeout=60).json().get("versions", [])
            except Exception:
                versions = []
            for v in versions:
                try:
                    cs = s.get(f"{self.base}/api/versions/{v['id']}/comments", timeout=60).json().get("comments", [])
                except Exception:
                    cs = []
                for c in cs:
                    out.append(self._pub(project, ent, c, "version", v, None))
            if include_task_comments:
                try:
                    tasks = s.get(f"{self.base}/api/entities/{ent['id']}/tasks", timeout=60).json().get("tasks", [])
                except Exception:
                    tasks = []
                for t in tasks:
                    # new task comments are most useful on active tasks; done tasks are still scanned lightly
                    try:
                        cs = s.get(f"{self.base}/api/tasks/{t['id']}/comments", timeout=60).json().get("comments", [])
                    except Exception:
                        cs = []
                    for c in cs:
                        out.append(self._pub(project, ent, c, "task", None, t))
            return out

        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            futures = {pool.submit(fetch_entity, e): e for e in entities}
            for i, fut in enumerate(as_completed(futures), 1):
                ent = futures[fut]
                try:
                    recs = fut.result()
                except Exception as exc:
                    if progress:
                        progress(f"error {ent.get('name')}: {exc}")
                    continue
                with lock:
                    records.extend(recs)
                if progress and (i % 10 == 0 or i == len(entities)):
                    progress(f"scanned entities {i}/{len(entities)}")
        if progress:
            progress(f"comments: {len(records)}")
        return records

    def _pub(self, project: dict, ent: dict, comment: dict, thread_kind: str,
             version: dict | None, task: dict | None) -> dict[str, Any]:
        attachments = []
        for a in comment.get("attachments") or []:
            attachments.append({
                "file_id": a.get("file_id"),
                "name": a.get("name"),
                "mime": a.get("mime"),
                "url": a.get("url"),
            })
        return {
            "project_id": project["id"],
            "project_alias": project.get("alias"),
            "entity_id": ent["id"],
            "shot": ent.get("name"),
            "thread_kind": thread_kind,
            "task_id": (task or {}).get("id"),
            "version_id": (version or {}).get("id"),
            "version_number": (version or {}).get("version_number"),
            "comment_id": comment.get("id"),
            "author": comment.get("author") or comment.get("author_full") or "",
            "body": comment.get("body") or "",
            "created_at": comment.get("created_at"),
            "attachments": attachments,
            "status": "new",
        }


def bot_client(base_url: str, bot: dict[str, Any]) -> SynxClient:
    return SynxClient(base_url, bot["login"], bot["password"])
