"""SQLite persistence layer."""
import sqlite3
import json
import time
from contextlib import contextmanager
from app.config import DB_PATH


SCHEMA = """
CREATE TABLE IF NOT EXISTS positions (
    ticker TEXT PRIMARY KEY,
    qty INTEGER NOT NULL,
    entry_cents REAL NOT NULL,
    target_cents REAL NOT NULL,
    stop_cents REAL NOT NULL,
    cost_basis_dollars REAL NOT NULL,
    asset_class TEXT,
    opened_at INTEGER NOT NULL,
    expires_at INTEGER
);

CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    side TEXT NOT NULL,
    qty INTEGER NOT NULL,
    limit_cents REAL,
    status TEXT NOT NULL,
    placed_at INTEGER NOT NULL,
    filled_at INTEGER
);

CREATE TABLE IF NOT EXISTS trade_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    body TEXT
);

CREATE TABLE IF NOT EXISTS signals_cache (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    cached_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS session_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


@contextmanager
def conn():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    try:
        yield c
        c.commit()
    finally:
        c.close()


def init_db():
    with conn() as c:
        c.executescript(SCHEMA)


def log(kind: str, title: str, body: str = ""):
    with conn() as c:
        c.execute(
            "INSERT INTO trade_log (ts, kind, title, body) VALUES (?, ?, ?, ?)",
            (int(time.time()), kind, title, body),
        )


def recent_log(limit: int = 25):
    with conn() as c:
        rows = c.execute(
            "SELECT ts, kind, title, body FROM trade_log ORDER BY ts DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


def cache_get(key: str, max_age_sec: int):
    with conn() as c:
        row = c.execute(
            "SELECT value, cached_at FROM signals_cache WHERE key = ?", (key,)
        ).fetchone()
        if not row:
            return None
        if time.time() - row["cached_at"] > max_age_sec:
            return None
        try:
            return json.loads(row["value"])
        except json.JSONDecodeError:
            return None


def cache_set(key: str, value):
    with conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO signals_cache (key, value, cached_at) VALUES (?, ?, ?)",
            (key, json.dumps(value), int(time.time())),
        )


def state_get(key: str, default=None):
    with conn() as c:
        row = c.execute(
            "SELECT value FROM session_state WHERE key = ?", (key,)
        ).fetchone()
        if not row:
            return default
        try:
            return json.loads(row["value"])
        except json.JSONDecodeError:
            return default


def state_set(key: str, value):
    with conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO session_state (key, value) VALUES (?, ?)",
            (key, json.dumps(value)),
        )


def all_positions():
    with conn() as c:
        rows = c.execute("SELECT * FROM positions").fetchall()
        return [dict(r) for r in rows]


def upsert_position(pos: dict):
    with conn() as c:
        c.execute(
            """INSERT OR REPLACE INTO positions
               (ticker, qty, entry_cents, target_cents, stop_cents,
                cost_basis_dollars, asset_class, opened_at, expires_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                pos["ticker"], pos["qty"], pos["entry_cents"],
                pos["target_cents"], pos["stop_cents"],
                pos["cost_basis_dollars"], pos.get("asset_class"),
                pos["opened_at"], pos.get("expires_at"),
            ),
        )


def delete_position(ticker: str):
    with conn() as c:
        c.execute("DELETE FROM positions WHERE ticker = ?", (ticker,))
