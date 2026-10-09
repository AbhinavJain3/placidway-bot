"""SQLite persistence for content-gap logging, leads and a light chat log.
Every write is also logged to stdout so it survives on hosts with ephemeral disks (e.g. Render free tier)."""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from pathlib import Path

log = logging.getLogger("storage")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS unanswered (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, session_id TEXT, question TEXT,
    search_query TEXT, language TEXT, status TEXT, top_sim REAL
);
CREATE TABLE IF NOT EXISTS leads (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, session_id TEXT, name TEXT, email TEXT,
    treatment TEXT, message TEXT
);
CREATE TABLE IF NOT EXISTS chat_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, session_id TEXT, question TEXT, intent TEXT,
    status TEXT, answer TEXT, sources TEXT, latency_ms INTEGER
);
"""


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class Storage:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(_SCHEMA)

    def _insert(self, table: str, row: dict) -> None:
        cols = ", ".join(row)
        marks = ", ".join("?" for _ in row)
        with self._lock:
            self._conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", tuple(row.values()))
            self._conn.commit()
        log.info("%s %s", table, json.dumps(row, ensure_ascii=False))

    def log_unanswered(self, **row) -> None:
        self._insert("unanswered", {"ts": _now(), **row})

    def save_lead(self, **row) -> None:
        self._insert("leads", {"ts": _now(), **row})

    def log_chat(self, **row) -> None:
        self._insert("chat_log", {"ts": _now(), **row})

    def recent(self, table: str, limit: int = 100) -> list[dict]:
        if table not in {"unanswered", "leads", "chat_log"}:
            raise ValueError(table)
        with self._lock:
            rows = self._conn.execute(f"SELECT * FROM {table} ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]
