"""SQLite state store. The Google Sheet is a mirror of this, not the source of truth."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from datetime import date
from pathlib import Path

from .models import Listing, Status

SCHEMA = """
CREATE TABLE IF NOT EXISTS listings (
    id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    address_key TEXT,
    first_seen REAL,
    updated REAL,
    data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS listings_status ON listings(status);
CREATE INDEX IF NOT EXISTS listings_address ON listings(address_key);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    listing_id TEXT,
    ts REAL NOT NULL,
    kind TEXT NOT NULL,
    payload TEXT
);
CREATE INDEX IF NOT EXISTS events_listing ON events(listing_id);

CREATE TABLE IF NOT EXISTS emails (
    message_id TEXT PRIMARY KEY,
    ts REAL NOT NULL,
    kind TEXT
);

CREATE TABLE IF NOT EXISTS variants (
    name TEXT PRIMARY KEY,
    language TEXT NOT NULL,
    style TEXT NOT NULL,
    wins REAL NOT NULL DEFAULT 0,
    losses REAL NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1,
    created REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS usage (
    day TEXT NOT NULL,
    model TEXT NOT NULL,
    usd REAL NOT NULL DEFAULT 0,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, model)
);

CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


class Store:
    def __init__(self, path: str | Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(SCHEMA)
        self._lock = threading.RLock()

    # --- listings -------------------------------------------------------------------------

    def get(self, listing_id: str) -> Listing | None:
        with self._lock:
            row = self._db.execute("SELECT data FROM listings WHERE id=?", (listing_id,)).fetchone()
        return Listing.model_validate_json(row[0]) if row else None

    def save(self, listing: Listing) -> None:
        listing.touch()
        with self._lock:
            self._db.execute(
                "INSERT INTO listings(id, status, address_key, first_seen, updated, data) VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET status=excluded.status, address_key=excluded.address_key, "
                "updated=excluded.updated, data=excluded.data",
                (listing.id, listing.status.value, listing.address_key, listing.first_seen,
                 listing.updated, listing.model_dump_json()),
            )
            self._db.commit()

    def insert_if_new(self, listing: Listing) -> bool:
        """Atomically claim a listing id. Returns False if it was already known."""
        with self._lock:
            cur = self._db.execute(
                "INSERT OR IGNORE INTO listings(id, status, address_key, first_seen, updated, data) "
                "VALUES(?,?,?,?,?,?)",
                (listing.id, listing.status.value, listing.address_key, listing.first_seen,
                 listing.updated, listing.model_dump_json()),
            )
            self._db.commit()
            return cur.rowcount == 1

    def all(self, statuses: set[Status] | None = None) -> list[Listing]:
        with self._lock:
            if statuses:
                marks = ",".join("?" * len(statuses))
                rows = self._db.execute(
                    f"SELECT data FROM listings WHERE status IN ({marks}) ORDER BY first_seen",
                    [s.value for s in statuses]).fetchall()
            else:
                rows = self._db.execute("SELECT data FROM listings ORDER BY first_seen").fetchall()
        return [Listing.model_validate_json(r[0]) for r in rows]

    def by_address_key(self, key: str) -> list[Listing]:
        if not key:
            return []
        with self._lock:
            rows = self._db.execute("SELECT data FROM listings WHERE address_key=?", (key,)).fetchall()
        return [Listing.model_validate_json(r[0]) for r in rows]

    # --- events ---------------------------------------------------------------------------

    def log(self, listing_id: str | None, kind: str, **payload) -> None:
        with self._lock:
            self._db.execute("INSERT INTO events(listing_id, ts, kind, payload) VALUES(?,?,?,?)",
                             (listing_id, time.time(), kind, json.dumps(payload, default=str)))
            self._db.commit()

    def events(self, since: float = 0, kind: str | None = None) -> list[dict]:
        query = "SELECT listing_id, ts, kind, payload FROM events WHERE ts>=?"
        args: list = [since]
        if kind:
            query += " AND kind=?"
            args.append(kind)
        with self._lock:
            rows = self._db.execute(query + " ORDER BY ts", args).fetchall()
        return [{"listing_id": r[0], "ts": r[1], "kind": r[2], **json.loads(r[3] or "{}")} for r in rows]

    def count_events(self, kind: str, since: float, **match) -> int:
        return sum(1 for e in self.events(since, kind) if all(e.get(k) == v for k, v in match.items()))

    # --- processed emails -----------------------------------------------------------------

    def email_seen(self, message_id: str) -> bool:
        with self._lock:
            return self._db.execute("SELECT 1 FROM emails WHERE message_id=?", (message_id,)).fetchone() is not None

    def mark_email(self, message_id: str, kind: str) -> None:
        with self._lock:
            self._db.execute("INSERT OR IGNORE INTO emails(message_id, ts, kind) VALUES(?,?,?)",
                             (message_id, time.time(), kind))
            self._db.commit()

    # --- message variants (learning) ------------------------------------------------------

    def variants(self, active_only: bool = True) -> list[dict]:
        query = "SELECT name, language, style, wins, losses, active, created FROM variants"
        if active_only:
            query += " WHERE active=1"
        with self._lock:
            rows = self._db.execute(query + " ORDER BY created").fetchall()
        keys = ("name", "language", "style", "wins", "losses", "active", "created")
        return [dict(zip(keys, r)) for r in rows]

    def add_variant(self, name: str, language: str, style: str) -> None:
        with self._lock:
            self._db.execute("INSERT OR IGNORE INTO variants(name, language, style, created) VALUES(?,?,?,?)",
                             (name, language, style, time.time()))
            self._db.commit()

    def record_outcome(self, variant: str, won: bool) -> None:
        column = "wins" if won else "losses"
        with self._lock:
            self._db.execute(f"UPDATE variants SET {column}={column}+1 WHERE name=?", (variant,))
            self._db.commit()

    def retire_variant(self, name: str) -> None:
        with self._lock:
            self._db.execute("UPDATE variants SET active=0 WHERE name=?", (name,))
            self._db.commit()

    # --- LLM spend ------------------------------------------------------------------------

    def add_usage(self, model: str, usd: float, input_tokens: int, output_tokens: int) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO usage(day, model, usd, input_tokens, output_tokens) VALUES(?,?,?,?,?) "
                "ON CONFLICT(day, model) DO UPDATE SET usd=usd+excluded.usd, "
                "input_tokens=input_tokens+excluded.input_tokens, output_tokens=output_tokens+excluded.output_tokens",
                (date.today().isoformat(), model, usd, input_tokens, output_tokens))
            self._db.commit()

    def spend_today(self) -> float:
        with self._lock:
            row = self._db.execute("SELECT COALESCE(SUM(usd),0) FROM usage WHERE day=?",
                                   (date.today().isoformat(),)).fetchone()
        return float(row[0])

    # --- key/value ------------------------------------------------------------------------

    def kv_get(self, key: str, default: str | None = None) -> str | None:
        with self._lock:
            row = self._db.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def kv_set(self, key: str, value: str) -> None:
        with self._lock:
            self._db.execute("INSERT INTO kv(key, value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                             (key, value))
            self._db.commit()
