"""Durable edit journal (SQLite).

The journal records what we intended, what we observed, and what we did, so a
restarted server can tell "nothing changed" from "changed, evidence lost"
without touching COM first. It is not a transaction over CATIA.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_OPERATIONS_TABLE = """
CREATE TABLE IF NOT EXISTS operations (
    session_id     TEXT NOT NULL,
    operation_id   TEXT NOT NULL,
    plan_hash      TEXT NOT NULL,
    state          TEXT NOT NULL,
    payload        TEXT NOT NULL,
    created_utc    TEXT NOT NULL,
    updated_utc    TEXT NOT NULL,
    PRIMARY KEY (session_id, operation_id)
)
"""

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS sessions (
    session_id     TEXT PRIMARY KEY,
    source_path    TEXT NOT NULL,
    working_path   TEXT NOT NULL,
    revision       INTEGER NOT NULL DEFAULT 0,
    status         TEXT NOT NULL,
    payload        TEXT NOT NULL,
    created_utc    TEXT NOT NULL,
    updated_utc    TEXT NOT NULL
);
{_OPERATIONS_TABLE};
CREATE INDEX IF NOT EXISTS operations_operation_id ON operations(operation_id);
CREATE TABLE IF NOT EXISTS events (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id     TEXT,
    operation_id   TEXT,
    kind           TEXT NOT NULL,
    payload        TEXT NOT NULL,
    created_utc    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_session ON events(session_id);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _migrate_operations_key(conn: sqlite3.Connection) -> None:
    """Move a journal from the old globally unique ``operation_id`` key to the
    ``(session_id, operation_id)`` composite key.

    Two sessions may legitimately use the same client-supplied operation id;
    the old schema let the second session overwrite the first session's row and
    replay its result. Existing rows are preserved, so recovery still sees the
    operations written before the upgrade.
    """
    if conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='operations'"
    ).fetchone() is None:
        return
    pk_columns = {row["name"] for row in conn.execute("PRAGMA table_info(operations)") if row["pk"]}
    if {"session_id", "operation_id"} <= pk_columns:
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("ALTER TABLE operations RENAME TO operations_legacy")
        conn.execute(_OPERATIONS_TABLE)
        conn.execute(
            """
            INSERT INTO operations
                (session_id, operation_id, plan_hash, state, payload, created_utc, updated_utc)
            SELECT session_id, operation_id, plan_hash, state, payload, created_utc, updated_utc
            FROM operations_legacy
            """
        )
        conn.execute("DROP TABLE operations_legacy")
    except Exception:
        conn.rollback()
        raise
    conn.commit()


def default_db_path() -> Path:
    env = os.environ.get("CATIA_MCP_EDIT_DB")
    if env:
        return Path(env)
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(base) / "catia-mcp" / "editing.sqlite3"


class Journal:
    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path else default_db_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._lock, self._connect() as conn:
            _migrate_operations_key(conn)
            conn.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
        return conn

    # -- sessions -----------------------------------------------------
    def save_session(self, session: dict[str, Any]) -> None:
        now = _now()
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO sessions (session_id, source_path, working_path, revision, status, payload, created_utc, updated_utc)
                VALUES (?,?,?,?,?,?,?,?)
                ON CONFLICT(session_id) DO UPDATE SET
                    revision=excluded.revision, status=excluded.status,
                    payload=excluded.payload, updated_utc=excluded.updated_utc
                """,
                (
                    session["editSessionId"],
                    session["source"]["path"],
                    session["workingCopy"]["path"],
                    int(session.get("modelRevision", 0)),
                    session.get("status", "ready"),
                    json.dumps(session, ensure_ascii=False),
                    session.get("createdUtc") or now,
                    now,
                ),
            )

    def load_session(self, session_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload FROM sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
        return json.loads(row["payload"]) if row else None

    def list_sessions(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT session_id, status, revision, working_path, updated_utc FROM sessions ORDER BY updated_utc DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    def delete_session(self, session_id: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))
            conn.execute("DELETE FROM events WHERE session_id = ?", (session_id,))
            conn.execute("DELETE FROM operations WHERE session_id = ?", (session_id,))

    # -- operations ---------------------------------------------------
    def get_operation(self, operation_id: str, session_id: str | None = None) -> dict[str, Any] | None:
        with self._connect() as conn:
            if session_id is None:
                row = conn.execute(
                    "SELECT * FROM operations WHERE operation_id = ?", (operation_id,)
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT * FROM operations WHERE session_id = ? AND operation_id = ?",
                    (session_id, operation_id),
                ).fetchone()
        if not row:
            return None
        data = dict(row)
        data["payload"] = json.loads(data["payload"])
        return data

    def upsert_operation(
        self, operation_id: str, session_id: str, plan_hash: str, state: str, payload: dict[str, Any]
    ) -> None:
        now = _now()
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO operations (session_id, operation_id, plan_hash, state, payload, created_utc, updated_utc)
                VALUES (?,?,?,?,?,?,?)
                ON CONFLICT(session_id, operation_id) DO UPDATE SET
                    plan_hash=excluded.plan_hash, state=excluded.state,
                    payload=excluded.payload, updated_utc=excluded.updated_utc
                """,
                (session_id, operation_id, plan_hash, state, json.dumps(payload, ensure_ascii=False), now, now),
            )

    def operations_in_state(self, session_id: str, states: tuple[str, ...]) -> list[dict[str, Any]]:
        marks = ",".join("?" for _ in states)
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM operations WHERE session_id = ? AND state IN ({marks}) ORDER BY created_utc",
                (session_id, *states),
            ).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item["payload"])
            out.append(item)
        return out

    # -- events -------------------------------------------------------
    def append_event(self, kind: str, payload: dict[str, Any], session_id: str | None = None, operation_id: str | None = None) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO events (session_id, operation_id, kind, payload, created_utc) VALUES (?,?,?,?,?)",
                (session_id, operation_id, kind, json.dumps(payload, ensure_ascii=False, default=str), _now()),
            )

    def events(self, session_id: str, limit: int = 200) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT kind, payload, created_utc FROM events WHERE session_id = ? ORDER BY id DESC LIMIT ?",
                (session_id, limit),
            ).fetchall()
        return [
            {"kind": r["kind"], "payload": json.loads(r["payload"]), "createdUtc": r["created_utc"]}
            for r in reversed(rows)
        ]
