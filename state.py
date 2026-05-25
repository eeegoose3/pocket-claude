"""Persistent runtime state helpers for pocket-claude.

SQLite is the primary store.  Legacy JSON files are still read once as a
migration source so existing local installs keep their chat/session bindings.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any

from backends import normalize_agent

STATE_DIR = os.path.dirname(os.path.abspath(__file__))
BIND_FILE = os.path.join(STATE_DIR, "bindings.json")
JSONL_ID_FILE = os.path.join(STATE_DIR, "jsonl_ids.json")
BACKEND_FILE = os.path.join(STATE_DIR, "session_backends.json")
DB_FILE = os.path.join(STATE_DIR, "bridge_state.db")
SCHEMA_VERSION = 1


@dataclass
class BridgeState:
    chat_session_map: dict[str, str] = field(default_factory=dict)
    session_jsonl_id: dict[str, str] = field(default_factory=dict)
    session_backend: dict[str, str] = field(default_factory=dict)
    remote_mode: dict[str, bool] = field(default_factory=dict)
    # Future-compatible runtime metadata, keyed by session name.
    # Values may include jsonl_path, jsonl_offset, last_message_id.
    session_runtime: dict[str, dict[str, Any]] = field(default_factory=dict)


def _load_json(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    with open(path, "r") as f:
        data = json.load(f)
    return data if isinstance(data, dict) else {}


def _db_for_paths(bind_file: str, jsonl_id_file: str, backend_file: str, db_file: str | None) -> str:
    if db_file:
        return db_file
    if (bind_file, jsonl_id_file, backend_file) == (BIND_FILE, JSONL_ID_FILE, BACKEND_FILE):
        return DB_FILE
    return os.path.join(os.path.dirname(os.path.abspath(bind_file)), "bridge_state.db")


def _connect(db_file: str) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(os.path.abspath(db_file)), exist_ok=True)
    conn = sqlite3.connect(db_file)
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS chat_bindings (
            chat_id TEXT PRIMARY KEY,
            session_name TEXT NOT NULL,
            updated_at REAL NOT NULL
        );

        CREATE TABLE IF NOT EXISTS session_backends (
            session_name TEXT PRIMARY KEY,
            backend TEXT NOT NULL,
            updated_at REAL NOT NULL
        );

        CREATE TABLE IF NOT EXISTS session_jsonl (
            session_name TEXT PRIMARY KEY,
            jsonl_id TEXT NOT NULL,
            updated_at REAL NOT NULL
        );

        CREATE TABLE IF NOT EXISTS session_runtime (
            session_name TEXT PRIMARY KEY,
            remote_mode INTEGER NOT NULL DEFAULT 0,
            jsonl_path TEXT,
            jsonl_offset INTEGER,
            last_message_id TEXT,
            updated_at REAL NOT NULL
        );
        """
    )
    conn.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', ?)",
        (str(SCHEMA_VERSION),),
    )
    conn.commit()


def _db_has_state(conn: sqlite3.Connection) -> bool:
    tables = ("chat_bindings", "session_backends", "session_jsonl", "session_runtime")
    for table in tables:
        count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        if count:
            return True
    return False


def _legacy_json_state(bind_file: str, jsonl_id_file: str, backend_file: str) -> BridgeState:
    backends = {k: normalize_agent(v) for k, v in _load_json(backend_file).items()}
    return BridgeState(
        chat_session_map={str(k): str(v) for k, v in _load_json(bind_file).items()},
        session_jsonl_id={str(k): str(v) for k, v in _load_json(jsonl_id_file).items()},
        session_backend=backends,
    )


def _load_sqlite(conn: sqlite3.Connection) -> BridgeState:
    chat_session_map = {
        row["chat_id"]: row["session_name"]
        for row in conn.execute("SELECT chat_id, session_name FROM chat_bindings")
    }
    session_backend = {
        row["session_name"]: normalize_agent(row["backend"])
        for row in conn.execute("SELECT session_name, backend FROM session_backends")
    }
    session_jsonl_id = {
        row["session_name"]: row["jsonl_id"]
        for row in conn.execute("SELECT session_name, jsonl_id FROM session_jsonl")
    }
    remote_mode: dict[str, bool] = {}
    session_runtime: dict[str, dict[str, Any]] = {}
    for row in conn.execute(
        "SELECT session_name, remote_mode, jsonl_path, jsonl_offset, last_message_id FROM session_runtime"
    ):
        sname = row["session_name"]
        remote_mode[sname] = bool(row["remote_mode"])
        runtime: dict[str, Any] = {}
        if row["jsonl_path"] is not None:
            runtime["jsonl_path"] = row["jsonl_path"]
        if row["jsonl_offset"] is not None:
            runtime["jsonl_offset"] = row["jsonl_offset"]
        if row["last_message_id"] is not None:
            runtime["last_message_id"] = row["last_message_id"]
        if runtime:
            session_runtime[sname] = runtime

    return BridgeState(
        chat_session_map=chat_session_map,
        session_jsonl_id=session_jsonl_id,
        session_backend=session_backend,
        remote_mode=remote_mode,
        session_runtime=session_runtime,
    )


def _save_sqlite(conn: sqlite3.Connection, state: BridgeState) -> None:
    now = time.time()
    with conn:
        conn.execute("DELETE FROM chat_bindings")
        conn.execute("DELETE FROM session_backends")
        conn.execute("DELETE FROM session_jsonl")
        conn.execute("DELETE FROM session_runtime")

        conn.executemany(
            "INSERT INTO chat_bindings(chat_id, session_name, updated_at) VALUES(?, ?, ?)",
            [(chat_id, sname, now) for chat_id, sname in state.chat_session_map.items()],
        )
        conn.executemany(
            "INSERT INTO session_backends(session_name, backend, updated_at) VALUES(?, ?, ?)",
            [(sname, normalize_agent(agent), now) for sname, agent in state.session_backend.items()],
        )
        conn.executemany(
            "INSERT INTO session_jsonl(session_name, jsonl_id, updated_at) VALUES(?, ?, ?)",
            [(sname, sid, now) for sname, sid in state.session_jsonl_id.items()],
        )

        runtime_sessions = set(state.remote_mode) | set(state.session_runtime)
        rows = []
        for sname in sorted(runtime_sessions):
            runtime = state.session_runtime.get(sname, {})
            rows.append((
                sname,
                1 if state.remote_mode.get(sname, False) else 0,
                runtime.get("jsonl_path"),
                runtime.get("jsonl_offset"),
                runtime.get("last_message_id"),
                now,
            ))
        conn.executemany(
            """
            INSERT INTO session_runtime(
                session_name, remote_mode, jsonl_path, jsonl_offset, last_message_id, updated_at
            ) VALUES(?, ?, ?, ?, ?, ?)
            """,
            rows,
        )


def load_state(
    bind_file: str = BIND_FILE,
    jsonl_id_file: str = JSONL_ID_FILE,
    backend_file: str = BACKEND_FILE,
    db_file: str | None = None,
) -> BridgeState:
    """Load bridge state from SQLite, migrating legacy JSON files on first use."""
    db_path = _db_for_paths(bind_file, jsonl_id_file, backend_file, db_file)
    conn = _connect(db_path)
    try:
        _ensure_schema(conn)
        if _db_has_state(conn):
            return _load_sqlite(conn)

        legacy = _legacy_json_state(bind_file, jsonl_id_file, backend_file)
        if legacy.chat_session_map or legacy.session_jsonl_id or legacy.session_backend:
            _save_sqlite(conn, legacy)
        return legacy
    finally:
        conn.close()


def save_state(
    state: BridgeState,
    bind_file: str = BIND_FILE,
    jsonl_id_file: str = JSONL_ID_FILE,
    backend_file: str = BACKEND_FILE,
    db_file: str | None = None,
) -> None:
    """Save bridge state to SQLite."""
    db_path = _db_for_paths(bind_file, jsonl_id_file, backend_file, db_file)
    conn = _connect(db_path)
    try:
        _ensure_schema(conn)
        _save_sqlite(conn, state)
    finally:
        conn.close()
