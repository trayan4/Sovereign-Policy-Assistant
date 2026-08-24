"""Stores governance relationships between documents (e.g. "POL-CASH-002
overrides POL-CASH-001") - the replacement for the old hand-authored
governs_note field. Two tables:

  relationships          Live, trusted relationships assess_node reads at
                          query time. Populated automatically by Stage 1
                          (a document explicitly declaring an override in
                          its own text) - self-declared, so no review
                          needed - and by human-approved rows promoted
                          from pending_relationships.

  pending_relationships   Candidate relationships Stage 2 (the similarity
                          scan) flags between two documents that were NOT
                          self-declared - i.e. the system noticed they
                          might conflict, but a human hasn't confirmed it
                          yet. Never read by assess_node directly."""

import sqlite3
from datetime import datetime, timezone

from app.config import CHROMA_PATH

RELATIONSHIPS_DB_PATH = CHROMA_PATH / "relationships.sqlite3"


def _connect():
    conn = sqlite3.connect(RELATIONSHIPS_DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS relationships (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_doc TEXT NOT NULL,
            target_doc TEXT NOT NULL,
            relationship TEXT NOT NULL,
            evidence TEXT,
            origin TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS pending_relationships (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_doc TEXT NOT NULL,
            target_doc TEXT NOT NULL,
            evidence TEXT,
            confidence REAL,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL,
            reviewed_at TEXT,
            reviewer_note TEXT
        )
    """)
    return conn


def add_relationship(source_doc: str, target_doc: str, relationship: str, evidence: str, origin: str) -> None:
    conn = _connect()
    conn.execute(
        "INSERT INTO relationships (source_doc, target_doc, relationship, evidence, origin, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (source_doc, target_doc, relationship, evidence, origin, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    conn.close()


def add_pending_relationship(source_doc: str, target_doc: str, evidence: str, confidence: float) -> None:
    conn = _connect()
    conn.execute(
        "INSERT INTO pending_relationships (source_doc, target_doc, evidence, confidence, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (source_doc, target_doc, evidence, confidence, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    conn.close()


def get_relationship_for_doc(doc_id: str) -> dict | None:
    """The one live relationship assess_node cares about: does this
    document declare it governs/overrides another? Returns None if not."""
    conn = _connect()
    row = conn.execute(
        "SELECT source_doc, target_doc, relationship, evidence FROM relationships WHERE source_doc = ? LIMIT 1",
        (doc_id,),
    ).fetchone()
    conn.close()
    if not row:
        return None
    return {"source_doc": row[0], "target_doc": row[1], "relationship": row[2], "evidence": row[3]}


def list_pending(status: str = "pending") -> list[dict]:
    conn = _connect()
    rows = conn.execute(
        "SELECT id, source_doc, target_doc, evidence, confidence, status, created_at "
        "FROM pending_relationships WHERE status = ? ORDER BY created_at",
        (status,),
    ).fetchall()
    conn.close()
    return [
        {
            "id": r[0], "source_doc": r[1], "target_doc": r[2], "evidence": r[3],
            "confidence": r[4], "status": r[5], "created_at": r[6],
        }
        for r in rows
    ]


def review_pending(pending_id: int, approve: bool, note: str | None = None) -> None:
    """Approving promotes the candidate into the live `relationships` table
    (in both directions is deliberately NOT assumed - only source_doc's
    stated direction is promoted, matching how Stage 1 records a direction
    too). Rejecting just marks it, leaving an audit trail either way."""
    conn = _connect()
    row = conn.execute(
        "SELECT source_doc, target_doc, evidence FROM pending_relationships WHERE id = ?",
        (pending_id,),
    ).fetchone()
    if not row:
        conn.close()
        raise ValueError(f"No pending relationship with id {pending_id}")

    new_status = "approved" if approve else "rejected"
    conn.execute(
        "UPDATE pending_relationships SET status = ?, reviewed_at = ?, reviewer_note = ? WHERE id = ?",
        (new_status, datetime.now(timezone.utc).isoformat(), note, pending_id),
    )
    conn.commit()
    conn.close()

    if approve:
        source_doc, target_doc, evidence = row
        add_relationship(source_doc, target_doc, "conflicts_with", evidence, origin="human_reviewed")
