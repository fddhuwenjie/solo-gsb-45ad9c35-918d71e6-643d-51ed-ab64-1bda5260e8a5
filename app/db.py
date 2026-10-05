"""SQLite storage for protocol declarations and idempotent decode results."""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

from .models import DecodeResponse, ProtocolDefinition

_SCHEMA = """
CREATE TABLE IF NOT EXISTS protocols (
    protocol_id   TEXT NOT NULL,
    version       INTEGER NOT NULL,
    definition    TEXT NOT NULL,
    definition_sha256 TEXT NOT NULL,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (protocol_id, version)
);

CREATE TABLE IF NOT EXISTS decode_cache (
    protocol_id     TEXT NOT NULL,
    version         INTEGER NOT NULL,
    input_sha256    TEXT NOT NULL,
    result          TEXT NOT NULL,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (protocol_id, version, input_sha256)
);

CREATE TABLE IF NOT EXISTS sessions (
    session_id        TEXT PRIMARY KEY,
    protocol_id       TEXT NOT NULL,
    version           INTEGER NOT NULL,
    received_offset   INTEGER NOT NULL,
    tail_b64          TEXT NOT NULL,
    frames_total      INTEGER NOT NULL DEFAULT 0,
    last_chunk_offset INTEGER,
    last_chunk_sha256 TEXT,
    last_response     TEXT NOT NULL,
    created_at        TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at        TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

_init_lock = threading.Lock()
_initialized: set[str] = set()


def _connect(db_path: str) -> sqlite3.Connection:
    if db_path != ":memory:":
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(db_path: str) -> None:
    with _init_lock:
        if db_path in _initialized:
            return
        conn = _connect(db_path)
        try:
            conn.executescript(_SCHEMA)
            conn.commit()
        finally:
            conn.close()
        _initialized.add(db_path)


class Database:
    def __init__(self, db_path: str) -> None:
        init_db(db_path)
        self._db_path = db_path
        self._local = threading.local()

    @property
    def conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = _connect(self._db_path)
            self._local.conn = conn
        return conn

    # ----- protocols -------------------------------------------------------

    def upsert_protocol(self, proto: ProtocolDefinition, definition_sha: str) -> None:
        payload = proto.model_dump_json(by_alias=True)
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO protocols "
                "(protocol_id, version, definition, definition_sha256) VALUES (?, ?, ?, ?)",
                (proto.protocol_id, proto.version, payload, definition_sha),
            )

    def get_protocol_row(self, protocol_id: str, version: int | None) -> sqlite3.Row | None:
        cur = self.conn.cursor()
        if version is not None:
            cur.execute(
                "SELECT * FROM protocols WHERE protocol_id = ? AND version = ?",
                (protocol_id, version),
            )
            return cur.fetchone()
        cur.execute(
            "SELECT * FROM protocols WHERE protocol_id = ? ORDER BY version DESC LIMIT 1",
            (protocol_id,),
        )
        return cur.fetchone()

    @staticmethod
    def row_to_protocol(row: sqlite3.Row) -> ProtocolDefinition:
        return ProtocolDefinition.model_validate(json.loads(row["definition"]))

    def find_protocol(self, protocol_id: str, version: int | None) -> tuple[ProtocolDefinition, int] | None:
        row = self.get_protocol_row(protocol_id, version)
        if row is None:
            return None
        return self.row_to_protocol(row), row["version"]

    def list_protocols(self) -> list[dict[str, Any]]:
        cur = self.conn.execute(
            "SELECT protocol_id, version, definition_sha256, created_at "
            "FROM protocols ORDER BY protocol_id, version"
        )
        return [dict(r) for r in cur.fetchall()]

    # ----- decode cache ----------------------------------------------------

    def get_cached(self, protocol_id: str, version: int, input_sha: str) -> dict | None:
        cur = self.conn.execute(
            "SELECT result FROM decode_cache WHERE protocol_id = ? AND version = ? AND input_sha256 = ?",
            (protocol_id, version, input_sha),
        )
        row = cur.fetchone()
        return json.loads(row["result"]) if row else None

    def put_cached(
        self, protocol_id: str, version: int, input_sha: str, result: DecodeResponse
    ) -> None:
        payload = result.model_dump_json()
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO decode_cache "
                "(protocol_id, version, input_sha256, result) VALUES (?, ?, ?, ?)",
                (protocol_id, version, input_sha, payload),
            )

    # ----- collection sessions ---------------------------------------------

    def create_session(self, session_id: str, protocol_id: str, version: int) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO sessions "
                "(session_id, protocol_id, version, received_offset, tail_b64, "
                " frames_total, last_chunk_offset, last_chunk_sha256, last_response) "
                "VALUES (?, ?, ?, 0, '', 0, NULL, NULL, '')",
                (session_id, protocol_id, version),
            )

    def get_session(self, session_id: str) -> sqlite3.Row | None:
        cur = self.conn.execute(
            "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
        )
        return cur.fetchone()

    def save_session_append(
        self,
        session_id: str,
        *,
        expected_received_offset: int,
        received_offset: int,
        tail_b64: str,
        frames_total: int,
        last_chunk_offset: int,
        last_chunk_sha: str,
        last_response_json: str,
    ) -> bool:
        """Durably commit one accepted append; returns True when it won.

        Offset, tail, frame count, last chunk and its response move together
        in a single statement, so they take effect as one state and a
        crash/restart never loses or duplicates work.

        The UPDATE doubles as a compare-and-swap: it only applies while the
        session's committed ``received_offset`` is still the one this append
        was computed from. ``received_offset`` grows strictly with every
        committed append (chunks are non-empty), so it acts as the state
        version. When several service instances share this database, the
        in-process session lock cannot serialise them; this guard can. It
        returns False when another writer committed first — the row is then
        left untouched and the caller re-reads the newest state to decide
        between replay and conflict.
        """
        with self.conn:
            cur = self.conn.execute(
                "UPDATE sessions SET "
                "received_offset = ?, tail_b64 = ?, frames_total = ?, "
                "last_chunk_offset = ?, last_chunk_sha256 = ?, last_response = ?, "
                "updated_at = datetime('now') "
                "WHERE session_id = ? AND received_offset = ?",
                (
                    received_offset,
                    tail_b64,
                    frames_total,
                    last_chunk_offset,
                    last_chunk_sha,
                    last_response_json,
                    session_id,
                    expected_received_offset,
                ),
            )
        return cur.rowcount == 1
