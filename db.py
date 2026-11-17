from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

DB_PATH = Path(__file__).resolve().parent / "data" / "robot.sqlite3"


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init() -> None:
    with connect() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS publications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id TEXT NOT NULL,
                project_alias TEXT,
                entity_id TEXT NOT NULL,
                shot TEXT,
                thread_kind TEXT,
                task_id TEXT,
                version_id TEXT,
                version_number INTEGER,
                comment_id TEXT NOT NULL UNIQUE,
                author TEXT,
                body TEXT,
                created_at TEXT,
                attachments_json TEXT DEFAULT '[]',
                status TEXT NOT NULL DEFAULT 'new',
                analysis_json TEXT,
                analysis_md TEXT,
                analysis_md_path TEXT,
                needs_action INTEGER,
                publication_type TEXT,
                analysis_source TEXT,
                answered_by_version INTEGER DEFAULT 0,
                answer_version_number INTEGER,
                answer_version_code TEXT,
                is_mention INTEGER DEFAULT 0,
                analysis_tokens INTEGER DEFAULT 0,
                created_db TEXT NOT NULL,
                updated_db TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_pub_status ON publications(status);
            CREATE INDEX IF NOT EXISTS idx_pub_entity ON publications(entity_id);

            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                publication_id INTEGER,
                synx_task_id TEXT,
                project_id TEXT,
                entity_id TEXT,
                shot TEXT,
                task_name TEXT,
                task_type TEXT,
                bot TEXT,
                status TEXT NOT NULL DEFAULT 'queued',
                stage TEXT NOT NULL DEFAULT 'queued',
                progress REAL DEFAULT 0,
                tokens_in INTEGER DEFAULT 0,
                tokens_out INTEGER DEFAULT 0,
                elapsed_sec REAL DEFAULT 0,
                prompt TEXT,
                negative TEXT,
                plan_json TEXT,
                plan_md TEXT,
                plan_md_path TEXT,
                workflow_json TEXT,
                source_json TEXT,
                work_dir TEXT,
                render_dir TEXT,
                result_file TEXT,
                version_id TEXT,
                version_code TEXT,
                synx_url TEXT,
                error TEXT,
                paused INTEGER DEFAULT 0,
                skipped INTEGER DEFAULT 0,
                priority INTEGER DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                started_at TEXT,
                finished_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status, id);
            CREATE INDEX IF NOT EXISTS idx_tasks_active ON tasks(status, paused, skipped);

            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER,
                kind TEXT,
                message TEXT,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT
            );

            CREATE TABLE IF NOT EXISTS chat_sessions (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS chat_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL DEFAULT 'default',
                role TEXT NOT NULL,
                content TEXT,
                images_json TEXT DEFAULT '[]',
                generated_json TEXT DEFAULT '[]',
                playlist_text TEXT,
                progress_json TEXT DEFAULT '[]',
                usage_json TEXT DEFAULT '{}',
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_chat_session ON chat_messages(session_id, id);

            CREATE TABLE IF NOT EXISTS project_views (
                session_id TEXT NOT NULL DEFAULT 'default',
                project_id TEXT NOT NULL,
                last_seen_id INTEGER DEFAULT 0,
                last_seen_at TEXT,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (session_id, project_id)
            );
            """
        )
        cols = {r["name"] for r in c.execute("PRAGMA table_info(tasks)").fetchall()}
        for col, typ in (("render_dir", "TEXT"), ("plan_md", "TEXT"), ("plan_md_path", "TEXT"),
                         ("version_code", "TEXT"), ("synx_url", "TEXT"),
                         ("priority", "INTEGER DEFAULT 0")):
            if col not in cols:
                c.execute(f"ALTER TABLE tasks ADD COLUMN {col} {typ}")
        pcols = {r["name"] for r in c.execute("PRAGMA table_info(publications)").fetchall()}
        for col, typ in (("analysis_md", "TEXT"), ("analysis_md_path", "TEXT"),
                         ("needs_action", "INTEGER"), ("publication_type", "TEXT"),
                         ("analysis_source", "TEXT"), ("answered_by_version", "INTEGER DEFAULT 0"),
                         ("answer_version_number", "INTEGER"), ("answer_version_code", "TEXT"),
                         ("is_mention", "INTEGER DEFAULT 0")):
            if col not in pcols:
                c.execute(f"ALTER TABLE publications ADD COLUMN {col} {typ}")
        ccols = {r["name"] for r in c.execute("PRAGMA table_info(chat_messages)").fetchall()}
        if "playlist_text" not in ccols:
            c.execute("ALTER TABLE chat_messages ADD COLUMN playlist_text TEXT")
        if "progress_json" not in ccols:
            c.execute("ALTER TABLE chat_messages ADD COLUMN progress_json TEXT DEFAULT '[]'")
        c.execute("INSERT OR IGNORE INTO chat_sessions (id,title,created_at,updated_at) VALUES ('default','Основной чат',?,?)", (now(), now()))
        c.commit()


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def one(sql: str, params: Iterable[Any] = ()) -> dict[str, Any] | None:
    with connect() as c:
        return row_to_dict(c.execute(sql, tuple(params)).fetchone())


def all(sql: str, params: Iterable[Any] = ()) -> list[dict[str, Any]]:
    with connect() as c:
        return [dict(r) for r in c.execute(sql, tuple(params)).fetchall()]


def execute(sql: str, params: Iterable[Any] = ()) -> int:
    with connect() as c:
        cur = c.execute(sql, tuple(params))
        c.commit()
        return int(cur.lastrowid or 0)


def add_publication(pub: dict[str, Any]) -> int | None:
    """Insert if comment_id is new. Returns new row id or None."""
    with connect() as c:
        try:
            cur = c.execute(
                """INSERT INTO publications
                   (project_id, project_alias, entity_id, shot, thread_kind, task_id,
                    version_id, version_number, comment_id, author, body, created_at,
                    attachments_json, status, needs_action, publication_type, analysis_source,
                    answered_by_version, answer_version_number, answer_version_code,
                    is_mention, created_db, updated_db)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    pub.get("project_id"), pub.get("project_alias"), pub.get("entity_id"),
                    pub.get("shot"), pub.get("thread_kind"), pub.get("task_id"),
                    pub.get("version_id"), pub.get("version_number"), pub["comment_id"],
                    pub.get("author"), pub.get("body"), pub.get("created_at"),
                    json.dumps(pub.get("attachments") or [], ensure_ascii=False),
                    pub.get("status", "new"),
                    0 if pub.get("answered_by_version") else (1 if pub.get("is_mention") else None),
                    "version" if pub.get("answered_by_version") else (
                        "continuation" if pub.get("is_mention") else None),
                    "reply" if pub.get("answered_by_version") else (
                        "mention" if pub.get("is_mention") else None),
                    int(pub.get("answered_by_version") or 0),
                    pub.get("answer_version_number"), pub.get("answer_version_code"),
                    int(pub.get("is_mention") or 0),
                    now(), now(),
                ),
            )
            c.commit()
            return int(cur.lastrowid or 0)
        except sqlite3.IntegrityError:
            return None


def update_publication_scan(rec: dict[str, Any]) -> None:
    """Refresh content/attachments for an existing comment from a full scan.

    The fast event monitor inserts comments without attachments; the next
    full project scan fills them in. Only actually changed rows are touched,
    so the UI incremental sync is not flooded.
    """
    comment_id = rec.get("comment_id")
    if not comment_id:
        return
    attachments = json.dumps(rec.get("attachments") or [], ensure_ascii=False)
    execute(
        """UPDATE publications
           SET thread_kind=?, task_id=?, version_id=?, version_number=?, author=?,
               body=?, created_at=?, attachments_json=?, updated_db=?
           WHERE comment_id=?
             AND (thread_kind IS NOT ? OR task_id IS NOT ? OR version_id IS NOT ?
                  OR version_number IS NOT ? OR author IS NOT ? OR body IS NOT ?
                  OR created_at IS NOT ? OR attachments_json IS NOT ?)""",
        (rec.get("thread_kind"), rec.get("task_id"), rec.get("version_id"),
         rec.get("version_number"), rec.get("author"), rec.get("body"),
         rec.get("created_at"), attachments, now(),
         comment_id,
         rec.get("thread_kind"), rec.get("task_id"), rec.get("version_id"),
         rec.get("version_number"), rec.get("author"), rec.get("body"),
         rec.get("created_at"), attachments),
    )


def sync_publication_answer(comment_id: str, answered: bool, version_number: int | None = None,
                            version_code: str | None = None) -> None:
    """Keep the "answered by version" flag in sync on repeated scans."""
    if answered:
        execute(
            """UPDATE publications
               SET answered_by_version=1, answer_version_number=?, answer_version_code=?,
                   needs_action=0, publication_type='version', analysis_source='reply',
                   updated_db=?
               WHERE comment_id=?
                 AND (answered_by_version IS NOT 1 OR answer_version_number IS NOT ?
                      OR needs_action IS NOT 0 OR publication_type IS NOT 'version')""",
            (version_number, version_code, now(), comment_id, version_number),
        )
    else:
        execute(
            """UPDATE publications
               SET answered_by_version=0, answer_version_number=NULL, answer_version_code=NULL,
                   needs_action=NULL, publication_type=NULL, analysis_source=NULL,
                   updated_db=?
               WHERE comment_id=? AND answered_by_version=1""",
            (now(), comment_id),
        )


def sync_publication_mention(comment_id: str, is_mention: bool) -> None:
    """Mark comments that address @p.robot. Mentions are actionable unless a
    version has already answered them, or AI has already classified them."""
    if not is_mention:
        return
    execute(
        """UPDATE publications
           SET is_mention=1,
               needs_action=CASE WHEN answered_by_version=1 THEN 0
                                 WHEN analysis_source='ai' THEN needs_action ELSE 1 END,
               publication_type=CASE WHEN answered_by_version=1 THEN 'version'
                                     WHEN analysis_source='ai' THEN publication_type
                                     ELSE 'continuation' END,
               analysis_source=CASE WHEN answered_by_version=1 THEN analysis_source
                                    WHEN analysis_source='ai' THEN 'ai' ELSE 'mention' END,
               updated_db=?
           WHERE comment_id=?
             AND (is_mention IS NOT 1
                  OR (answered_by_version IS NOT 1 AND analysis_source IS NOT 'ai'
                      AND needs_action IS NOT 1))""",
        (now(), comment_id),
    )


def _pub_row(r: dict[str, Any]) -> dict[str, Any]:
    try:
        r["attachments"] = json.loads(r.get("attachments_json") or "[]")
    except Exception:
        r["attachments"] = []
    try:
        r["analysis"] = json.loads(r.get("analysis_json") or "null")
    except Exception:
        r["analysis"] = None
    return r


def list_publications(status: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
    if status:
        rows = all("SELECT * FROM publications WHERE status=? ORDER BY created_at DESC, id DESC LIMIT ?",
                   (status, limit))
    else:
        rows = all("SELECT * FROM publications ORDER BY created_at DESC, id DESC LIMIT ?", (limit,))
    return [_pub_row(r) for r in rows]


def publications_changes(since: str, since_id: int = 0, limit: int = 1000) -> list[dict[str, Any]]:
    if not since:
        return []
    rows = all(
        """SELECT * FROM publications
           WHERE updated_db > ? OR (updated_db = ? AND id > ?)
           ORDER BY updated_db ASC, id ASC LIMIT ?""",
        (since, since, int(since_id or 0), int(limit)))
    return [_pub_row(r) for r in rows]


def project_publications(project_id: str, date_from: str, date_to: str,
                         limit: int = 3000) -> list[dict[str, Any]]:
    rows = all(
        """SELECT * FROM publications
           WHERE project_id=? AND substr(created_at,1,10) >= ? AND substr(created_at,1,10) <= ?
           ORDER BY created_at DESC, id DESC LIMIT ?""",
        (project_id, date_from, date_to, int(limit)))
    return [_pub_row(r) for r in rows]


def get_project_view(session_id: str, project_id: str) -> dict[str, Any] | None:
    return one("SELECT * FROM project_views WHERE session_id=? AND project_id=?",
               (session_id, project_id))


def set_project_view(session_id: str, project_id: str, last_seen_id: int,
                     last_seen_at: str | None) -> None:
    execute(
        """INSERT INTO project_views (session_id, project_id, last_seen_id, last_seen_at, updated_at)
           VALUES (?,?,?,?,?)
           ON CONFLICT(session_id, project_id) DO UPDATE SET
             last_seen_id=excluded.last_seen_id,
             last_seen_at=excluded.last_seen_at,
             updated_at=excluded.updated_at""",
        (session_id, project_id, int(last_seen_id or 0), last_seen_at, now()))


def get_publication(pid: int) -> dict[str, Any] | None:
    r = one("SELECT * FROM publications WHERE id=?", (pid,))
    return _pub_row(r) if r else None


def update_publication(pid: int, **fields: Any) -> None:
    if not fields:
        return
    fields["updated_db"] = now()
    keys = ", ".join(f"{k}=?" for k in fields)
    execute(f"UPDATE publications SET {keys} WHERE id=?", (*fields.values(), pid))


def create_task(data: dict[str, Any]) -> int:
    ts = now()
    keys = [
        "publication_id", "synx_task_id", "project_id", "entity_id", "shot",
        "task_name", "task_type", "bot", "status", "stage", "progress",
        "tokens_in", "tokens_out", "elapsed_sec", "prompt", "negative",
        "plan_json", "plan_md", "plan_md_path", "workflow_json", "source_json",
        "work_dir", "render_dir", "result_file",
        "version_id", "version_code", "synx_url", "error", "paused", "skipped",
        "priority",
    ]
    defaults = {"status": "queued", "stage": "queued", "progress": 0,
                "paused": 0, "skipped": 0, "priority": 0}
    values = []
    for k in keys:
        v = data.get(k)
        if v is None and k in defaults:
            v = defaults[k]
        values.append(v)
    fields = ", ".join(keys + ["created_at", "updated_at"])
    marks = ", ".join(["?"] * (len(keys) + 2))
    return execute(f"INSERT INTO tasks ({fields}) VALUES ({marks})", (*values, ts, ts))


def list_tasks(limit: int = 500) -> list[dict[str, Any]]:
    rows = all("SELECT * FROM tasks ORDER BY id DESC LIMIT ?", (limit,))
    for r in rows:
        for k in ("plan_json", "workflow_json", "source_json"):
            if r.get(k):
                try:
                    r[k] = json.loads(r[k])
                except Exception:
                    pass
    return rows


def get_task(tid: int) -> dict[str, Any] | None:
    r = one("SELECT * FROM tasks WHERE id=?", (tid,))
    if not r:
        return None
    for k in ("plan_json", "workflow_json", "source_json"):
        if r.get(k):
            try:
                r[k] = json.loads(r[k])
            except Exception:
                pass
    return r


def update_task(tid: int, **fields: Any) -> None:
    if not fields:
        return
    fields["updated_at"] = now()
    keys = ", ".join(f"{k}=?" for k in fields)
    execute(f"UPDATE tasks SET {keys} WHERE id=?", (*fields.values(), tid))


def active_task() -> dict[str, Any] | None:
    r = one("SELECT * FROM tasks WHERE status='running' ORDER BY id DESC LIMIT 1")
    if r:
        for k in ("plan_json", "workflow_json", "source_json"):
            if r.get(k):
                try:
                    r[k] = json.loads(r[k])
                except Exception:
                    pass
    return r


def next_task() -> dict[str, Any] | None:
    r = one(
        """SELECT * FROM tasks
           WHERE status='queued' AND paused=0 AND skipped=0
           ORDER BY COALESCE(priority, 0) DESC, id ASC LIMIT 1"""
    )
    if not r:
        return None
    for k in ("plan_json", "workflow_json", "source_json"):
        if r.get(k):
            try:
                r[k] = json.loads(r[k])
            except Exception:
                pass
    return r


def delete_task(tid: int) -> None:
    execute("DELETE FROM tasks WHERE id=?", (tid,))


def delete_done() -> int:
    with connect() as c:
        cur = c.execute("DELETE FROM tasks WHERE status='done'")
        c.commit()
        return int(cur.rowcount or 0)


def add_event(task_id: int | None, kind: str, message: str) -> None:
    execute("INSERT INTO events (task_id, kind, message, created_at) VALUES (?,?,?,?)",
            (task_id, kind, message, now()))


def set_meta(key: str, value: str) -> None:
    execute("INSERT INTO meta (key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value))


def get_meta(key: str, default: str | None = None) -> str | None:
    r = one("SELECT value FROM meta WHERE key=?", (key,))
    return r["value"] if r else default


def add_chat_message(session_id: str, role: str, content: str,
                     images: list[dict[str, Any]] | None = None,
                     generated: list[dict[str, Any]] | None = None,
                     usage: dict[str, Any] | None = None,
                     playlist_text: str | None = None,
                     progress: list[dict[str, Any]] | None = None) -> int:
    execute("INSERT OR IGNORE INTO chat_sessions (id,title,created_at,updated_at) VALUES (?,?,?,?)",
            (session_id, "Новый чат", now(), now()))
    tid = execute(
        """INSERT INTO chat_messages
           (session_id, role, content, images_json, generated_json, playlist_text, progress_json, usage_json, created_at)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (session_id, role, content or "", json.dumps(images or [], ensure_ascii=False),
         json.dumps(generated or [], ensure_ascii=False), playlist_text or None,
         json.dumps(progress or [], ensure_ascii=False),
         json.dumps(usage or {}, ensure_ascii=False), now()),
    )
    execute("UPDATE chat_sessions SET updated_at=? WHERE id=?", (now(), session_id))
    return tid


def list_chat_messages(session_id: str = "default", limit: int = 200) -> list[dict[str, Any]]:
    rows = all("SELECT * FROM chat_messages WHERE session_id=? ORDER BY id ASC LIMIT ?",
               (session_id, limit))
    for r in rows:
        for src, dst in (("images_json", "images"), ("generated_json", "generated"),
                         ("usage_json", "usage"), ("progress_json", "progress")):
            try:
                r[dst] = json.loads(r.get(src) or ("[]" if dst != "usage" else "{}"))
            except Exception:
                r[dst] = [] if dst != "usage" else {}
    return rows


def clear_chat(session_id: str = "default") -> None:
    execute("DELETE FROM chat_messages WHERE session_id=?", (session_id,))


def list_chat_sessions() -> list[dict[str, Any]]:
    return all("SELECT * FROM chat_sessions ORDER BY updated_at DESC, created_at DESC")


def create_chat_session(title: str) -> dict[str, Any]:
    import uuid
    sid = uuid.uuid4().hex[:16]
    execute("INSERT INTO chat_sessions (id,title,created_at,updated_at) VALUES (?,?,?,?)",
            (sid, title or "Новый чат", now(), now()))
    return one("SELECT * FROM chat_sessions WHERE id=?", (sid,))


def delete_chat_session(session_id: str) -> None:
    execute("DELETE FROM chat_messages WHERE session_id=?", (session_id,))
    execute("DELETE FROM chat_sessions WHERE id=?", (session_id,))


def rename_chat_session(session_id: str, title: str) -> None:
    execute("UPDATE chat_sessions SET title=?, updated_at=? WHERE id=?",
            (title, now(), session_id))
