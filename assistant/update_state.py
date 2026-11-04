"""Durable update requests for the isolated pilot.

Uses sqlite on MEDIA_ROOT. The directory MUST be a persistent Railway volume to
survive redeployments. Never automatically replay an interrupted Sheets write.
"""
import datetime
from contextlib import contextmanager
import os
import sqlite3
import threading
from zoneinfo import ZoneInfo

DB = os.path.join(os.environ.get("MEDIA_ROOT", "media"), "project_update_requests.sqlite3")
_LOCK = threading.RLock()


@contextmanager
def _connect():
    os.makedirs(os.path.dirname(os.path.abspath(DB)), exist_ok=True)
    connection = sqlite3.connect(DB, timeout=15)
    connection.execute("PRAGMA busy_timeout=15000")
    connection.execute("""CREATE TABLE IF NOT EXISTS updates (
        key TEXT PRIMARY KEY,
        chat_id INTEGER NOT NULL,
        message_id INTEGER NOT NULL,
        project TEXT NOT NULL,
        source_text TEXT NOT NULL,
        state TEXT NOT NULL,
        proposal_json TEXT NOT NULL DEFAULT '',
        detail TEXT NOT NULL DEFAULT '',
        updated_at TEXT NOT NULL
    )""")
    try:
        yield connection
        connection.commit()
    finally:
        connection.close()


def _now():
    return datetime.datetime.now(ZoneInfo("Europe/Moscow")).isoformat(timespec="seconds")


def start(chat_id, message_id, project, source_text):
    key = f"{chat_id}:{message_id}"
    with _LOCK, _connect() as db:
        db.execute("INSERT OR IGNORE INTO updates(key,chat_id,message_id,project,source_text,state,updated_at) VALUES (?,?,?,?,?,?,?)",
                   (key, chat_id, message_id, project, source_text, "received", _now()))
    # Read only after the insertion transaction commits.
    return get(key)


def get(key):
    with _LOCK, _connect() as db:
        db.row_factory = sqlite3.Row
        row = db.execute("SELECT * FROM updates WHERE key=?", (key,)).fetchone()
        return dict(row) if row else None


def latest(chat_id):
    with _LOCK, _connect() as db:
        db.row_factory = sqlite3.Row
        row = db.execute("SELECT * FROM updates WHERE chat_id=? ORDER BY updated_at DESC, message_id DESC LIMIT 1",
                         (chat_id,)).fetchone()
        return dict(row) if row else None


def save(key, state, proposal_json=None, detail=""):
    with _LOCK, _connect() as db:
        if proposal_json is None:
            db.execute("UPDATE updates SET state=?,detail=?,updated_at=? WHERE key=?",
                       (state, detail[:1200], _now(), key))
        else:
            db.execute("UPDATE updates SET state=?,proposal_json=?,detail=?,updated_at=? WHERE key=?",
                       (state, proposal_json, detail[:1200], _now(), key))


def claim_apply(key):
    """Single-winner CAS to prevent double-click and concurrent writes.

    A crash after the claim leaves state 'applying'; manual reconciliation needed.
    """
    with _LOCK, _connect() as db:
        result = db.execute("UPDATE updates SET state='applying', updated_at=? WHERE key=? AND state='ready'",
                            (_now(), key))
        return result.rowcount == 1
