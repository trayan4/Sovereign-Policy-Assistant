"""User-initiated service requests, raised directly from a refused answer
(out_of_scope or expired) - distinct from the automatic, silent escalation
logging in query_log.py, which keeps happening exactly as before whether
or not a user does anything about it. This is the active, visible
follow-up path: a real reference number, a known requester (from the
validated login, not typed in), an urgency level, and a status a
compliance admin can actually resolve - closing the loop that
escalations.resolved never did (see query_log.py)."""

import sqlite3
from datetime import datetime, timezone

from app.config import QUERY_LOG_PATH


def _connect():
    conn = sqlite3.connect(QUERY_LOG_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS service_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            reference TEXT UNIQUE,
            question TEXT NOT NULL,
            case_type TEXT NOT NULL,
            urgency TEXT NOT NULL,
            note TEXT,
            requester TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'open',
            created_at TEXT NOT NULL,
            resolved_at TEXT
        )
    """)
    return conn


def raise_sr(question: str, case_type: str, urgency: str, note: str | None, requester: str) -> str:
    """Creates a new SR and returns its reference (SR-<year>-<6-digit id>).
    The reference is derived from the row's own id after insert, not a
    separate counter - guaranteed unique without anything else to keep in
    sync or that could drift out of step with the actual row count."""
    conn = _connect()
    now = datetime.now(timezone.utc)
    cur = conn.execute(
        "INSERT INTO service_requests "
        "(reference, question, case_type, urgency, note, requester, status, created_at) "
        "VALUES (NULL, ?, ?, ?, ?, ?, 'open', ?)",
        (question, case_type, urgency, note, requester, now.isoformat()),
    )
    sr_id = cur.lastrowid
    reference = f"SR-{now.year}-{sr_id:06d}"
    conn.execute("UPDATE service_requests SET reference = ? WHERE id = ?", (reference, sr_id))
    conn.commit()
    conn.close()
    return reference


def list_open() -> list[dict]:
    conn = _connect()
    rows = conn.execute(
        "SELECT id, reference, question, case_type, urgency, note, requester, status, created_at "
        "FROM service_requests WHERE status = 'open' ORDER BY created_at"
    ).fetchall()
    conn.close()
    return [
        {
            "id": r[0], "reference": r[1], "question": r[2], "case_type": r[3],
            "urgency": r[4], "note": r[5], "requester": r[6], "status": r[7], "created_at": r[8],
        }
        for r in rows
    ]


def resolve(sr_id: int) -> None:
    conn = _connect()
    cur = conn.execute(
        "UPDATE service_requests SET status = 'resolved', resolved_at = ? WHERE id = ? AND status = 'open'",
        (datetime.now(timezone.utc).isoformat(), sr_id),
    )
    conn.commit()
    conn.close()
    if cur.rowcount == 0:
        raise ValueError(f"No open service request with id {sr_id}")
