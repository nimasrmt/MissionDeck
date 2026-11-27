"""SQLite persistence for MissionDeck."""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from app.config import DB_PATH, RUNS_DIR, WORKER_CAP_DEFAULT


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


_local = threading.local()


def ensure_runtime_dirs() -> None:
    RUNS_DIR.mkdir(parents=True, exist_ok=True)


def connect(db_path: Path | None = None) -> sqlite3.Connection:
    ensure_runtime_dirs()
    path = db_path or DB_PATH
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def get_conn(db_path: Path | None = None) -> sqlite3.Connection:
    key = str(db_path or DB_PATH)
    store = getattr(_local, "conns", None)
    if store is None:
        store = {}
        _local.conns = store
    conn = store.get(key)
    if conn is None:
        conn = connect(db_path)
        store[key] = conn
    return conn


@contextmanager
def transaction(db_path: Path | None = None) -> Iterator[sqlite3.Connection]:
    conn = get_conn(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


SCHEMA = """
CREATE TABLE IF NOT EXISTS missions (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    objective TEXT NOT NULL,
    quality_requirements TEXT NOT NULL,
    scope TEXT NOT NULL,
    constraints TEXT NOT NULL,
    coordinator TEXT NOT NULL,
    workspace TEXT NOT NULL,
    status TEXT NOT NULL,
    plan_json TEXT,
    plan_version INTEGER NOT NULL DEFAULT 0,
    plan_status TEXT NOT NULL DEFAULT 'none',
    plan_errors_json TEXT,
    run_id TEXT,
    planner_dispatch_id TEXT,
    repair_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    approved_at TEXT,
    cancel_requested INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    mission_id TEXT NOT NULL,
    plan_task_id TEXT NOT NULL,
    title TEXT NOT NULL,
    role TEXT NOT NULL,
    agent TEXT NOT NULL,
    model TEXT,
    deps_json TEXT NOT NULL,
    write_scope_json TEXT NOT NULL,
    outputs_json TEXT NOT NULL,
    acceptance_json TEXT NOT NULL,
    validation_json TEXT NOT NULL,
    review_required INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    native_task_id TEXT,
    dispatch_id TEXT,
    attempt INTEGER NOT NULL DEFAULT 1,
    result_summary TEXT,
    evidence_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    started_at TEXT,
    ended_at TEXT,
    FOREIGN KEY(mission_id) REFERENCES missions(id)
);

CREATE TABLE IF NOT EXISTS dispatches (
    id TEXT PRIMARY KEY,
    mission_id TEXT NOT NULL,
    task_id TEXT,
    native_dispatch_id TEXT NOT NULL,
    native_task_id TEXT,
    agent TEXT,
    model TEXT,
    state TEXT,
    liveness TEXT,
    terminal_state TEXT,
    last_seen_at TEXT,
    next_action TEXT,
    raw_json TEXT,
    FOREIGN KEY(mission_id) REFERENCES missions(id)
);

CREATE TABLE IF NOT EXISTS artifacts (
    id TEXT PRIMARY KEY,
    mission_id TEXT NOT NULL,
    task_id TEXT,
    rel_path TEXT NOT NULL,
    size INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    kind TEXT NOT NULL,
    verified INTEGER NOT NULL DEFAULT 0,
    validation_output TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY(mission_id) REFERENCES missions(id)
);

CREATE TABLE IF NOT EXISTS events (
    id TEXT PRIMARY KEY,
    mission_id TEXT,
    ts TEXT NOT NULL,
    type TEXT NOT NULL,
    message TEXT NOT NULL,
    data_json TEXT
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS manager_turns (
    id TEXT PRIMARY KEY,
    parent_mission_id TEXT NOT NULL,
    block_id TEXT NOT NULL,
    request_id TEXT NOT NULL,
    child_mission_id TEXT NOT NULL,
    message TEXT NOT NULL,
    advisors_json TEXT NOT NULL,
    reply_path TEXT NOT NULL,
    reply_data_json TEXT,
    status TEXT NOT NULL,
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(parent_mission_id, request_id),
    FOREIGN KEY(parent_mission_id) REFERENCES missions(id),
    FOREIGN KEY(child_mission_id) REFERENCES missions(id)
);
CREATE INDEX IF NOT EXISTS manager_turn_parent ON manager_turns(parent_mission_id, created_at);

CREATE TABLE IF NOT EXISTS audit (
    id TEXT PRIMARY KEY,
    ts TEXT NOT NULL,
    action TEXT NOT NULL,
    detail_json TEXT
);
"""


def init_db(db_path: Path | None = None) -> None:
    conn = get_conn(db_path)
    conn.executescript(SCHEMA)
    defaults = {
        "worker_cap": str(WORKER_CAP_DEFAULT),
        "auto_refresh_seconds": "0",
        "redaction_enabled": "true",
        "coordinator_terminal": "",
    }
    for key, value in defaults.items():
        conn.execute(
            "INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)",
            (key, value),
        )
    conn.commit()


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return dict(row)


def dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def loads(text: str | None, default: Any = None) -> Any:
    if text is None or text == "":
        return default
    return json.loads(text)


def get_setting(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    if row is None:
        return default
    return row["value"]


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def add_event(
    conn: sqlite3.Connection,
    mission_id: str | None,
    event_type: str,
    message: str,
    data: dict | None = None,
) -> str:
    eid = new_id("evt")
    conn.execute(
        "INSERT INTO events(id, mission_id, ts, type, message, data_json) VALUES (?, ?, ?, ?, ?, ?)",
        (eid, mission_id, utc_now(), event_type, message, dumps(data) if data is not None else None),
    )
    return eid


def add_audit(conn: sqlite3.Connection, action: str, detail: dict | None = None) -> str:
    aid = new_id("aud")
    conn.execute(
        "INSERT INTO audit(id, ts, action, detail_json) VALUES (?, ?, ?, ?)",
        (aid, utc_now(), action, dumps(detail) if detail is not None else None),
    )
    return aid


def write_checkpoint(mission_id: str, label: str, payload: dict[str, Any]) -> Path:
    """Write a checkpoint file under .orca-runs/<mission_id>/."""
    dest = RUNS_DIR / mission_id
    dest.mkdir(parents=True, exist_ok=True)
    path = dest / f"checkpoint-{label}-{utc_now().replace(':', '')}.json"
    path.write_text(dumps({"label": label, "ts": utc_now(), **payload}), encoding="utf-8")
    return path
