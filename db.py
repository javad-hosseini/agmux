import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional
import aiosqlite

DEFAULT_DATA_DIR = Path(os.getenv("DATA_DIR", "./data"))
DEFAULT_DB_PATH = DEFAULT_DATA_DIR / "state.db"

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS sessions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    project_slug  TEXT NOT NULL,
    agy_conv_id   TEXT,
    title         TEXT NOT NULL,
    mode          TEXT DEFAULT 'PLAN',
    status        TEXT DEFAULT 'IDLE',
    last_prompt   TEXT,
    created_at    DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at    DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS user_context (
    telegram_user_id  INTEGER PRIMARY KEY,
    active_project    TEXT NOT NULL,
    active_session_id INTEGER,
    FOREIGN KEY (active_session_id) REFERENCES sessions(id)
);

CREATE TABLE IF NOT EXISTS audit_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id    INTEGER,
    event_type    TEXT NOT NULL,
    payload       TEXT NOT NULL,
    created_at    DATETIME DEFAULT CURRENT_TIMESTAMP
);
"""

_current_db_path: Path = DEFAULT_DB_PATH


def get_db_path() -> Path:
    return _current_db_path


def set_db_path(path: Path | str) -> None:
    global _current_db_path
    _current_db_path = Path(path)
    _current_db_path.parent.mkdir(parents=True, exist_ok=True)


async def init_db(db_path: Optional[Path | str] = None) -> None:
    if db_path is not None:
        set_db_path(db_path)
    else:
        get_db_path().parent.mkdir(parents=True, exist_ok=True)

    async with aiosqlite.connect(get_db_path()) as db:
        await db.executescript(SCHEMA)
        await db.commit()


async def get_or_create_context(user_id: int, default_project: str) -> Dict[str, Any]:
    async with aiosqlite.connect(get_db_path()) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM user_context WHERE telegram_user_id = ?", (user_id,)
        ) as cur:
            row = await cur.fetchone()
            if row:
                return dict(row)

        await db.execute(
            "INSERT INTO user_context (telegram_user_id, active_project, active_session_id) VALUES (?, ?, NULL)",
            (user_id, default_project),
        )
        await db.commit()
        return {
            "telegram_user_id": user_id,
            "active_project": default_project,
            "active_session_id": None,
        }


async def set_active_project(
    user_id: int, project_slug: str, session_id: Optional[int] = None
) -> None:
    async with aiosqlite.connect(get_db_path()) as db:
        await db.execute(
            """
            INSERT INTO user_context (telegram_user_id, active_project, active_session_id)
            VALUES (?, ?, ?)
            ON CONFLICT(telegram_user_id) DO UPDATE SET
                active_project = excluded.active_project,
                active_session_id = excluded.active_session_id
            """,
            (user_id, project_slug, session_id),
        )
        await db.commit()


async def set_active_session(user_id: int, session_id: int) -> None:
    async with aiosqlite.connect(get_db_path()) as db:
        await db.execute(
            "UPDATE user_context SET active_session_id = ? WHERE telegram_user_id = ?",
            (session_id, user_id),
        )
        await db.commit()


async def create_session(
    user_id: int, project_slug: str, title: str, mode: str = "PLAN"
) -> int:
    async with aiosqlite.connect(get_db_path()) as db:
        cur = await db.execute(
            "INSERT INTO sessions (project_slug, title, mode, status) VALUES (?, ?, ?, 'IDLE')",
            (project_slug, title[:80] if title else "Untitled session", mode),
        )
        session_id = cur.lastrowid
        await db.execute(
            """
            INSERT INTO user_context (telegram_user_id, active_project, active_session_id)
            VALUES (?, ?, ?)
            ON CONFLICT(telegram_user_id) DO UPDATE SET
                active_project = excluded.active_project,
                active_session_id = excluded.active_session_id
            """,
            (user_id, project_slug, session_id),
        )
        await db.commit()
        return session_id


async def get_session(session_id: int) -> Optional[Dict[str, Any]]:
    async with aiosqlite.connect(get_db_path()) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def update_session(session_id: int, **fields: Any) -> None:
    if not fields:
        return
    fields["updated_at"] = "CURRENT_TIMESTAMP"
    col_clauses = []
    vals = []
    for k, v in fields.items():
        if k == "updated_at":
            col_clauses.append("updated_at = CURRENT_TIMESTAMP")
        else:
            col_clauses.append(f"{k} = ?")
            vals.append(v)
    vals.append(session_id)

    query = f"UPDATE sessions SET {', '.join(col_clauses)} WHERE id = ?"
    async with aiosqlite.connect(get_db_path()) as db:
        await db.execute(query, vals)
        await db.commit()


async def list_sessions(project_slug: str, limit: int = 10) -> List[Dict[str, Any]]:
    async with aiosqlite.connect(get_db_path()) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM sessions WHERE project_slug = ? ORDER BY id DESC LIMIT ?",
            (project_slug, limit),
        ) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]


async def log_audit(
    session_id: Optional[int], event_type: str, payload: Any
) -> None:
    payload_str = payload if isinstance(payload, str) else json.dumps(payload, default=str)
    async with aiosqlite.connect(get_db_path()) as db:
        await db.execute(
            "INSERT INTO audit_log (session_id, event_type, payload) VALUES (?, ?, ?)",
            (session_id, event_type, payload_str),
        )
        await db.commit()

