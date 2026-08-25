import json
import sqlite3
from datetime import datetime, timezone

from app.config import QUERY_LOG_PATH


def _connect():
    conn = sqlite3.connect(QUERY_LOG_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS queries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            department TEXT,
            language TEXT,
            question TEXT NOT NULL,
            case_type TEXT NOT NULL,
            doc_ids TEXT,
            answer TEXT,
            total_tokens INTEGER
        )
    """)
    # A database created before total_tokens existed won't have the column -
    # CREATE TABLE IF NOT EXISTS above is a no-op against an existing table,
    # so this ALTER TABLE is what actually adds it for those. Cheap no-op
    # (caught and ignored) once the column is already there.
    try:
        conn.execute("ALTER TABLE queries ADD COLUMN total_tokens INTEGER")
    except sqlite3.OperationalError:
        pass
    conn.execute("""
        CREATE TABLE IF NOT EXISTS escalations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            query_id INTEGER NOT NULL,
            timestamp TEXT NOT NULL,
            reason TEXT NOT NULL,
            owner TEXT,
            resolved INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY (query_id) REFERENCES queries(id)
        )
    """)
    return conn


def log_query(
    department: str | None,
    language: str,
    question: str,
    case_type: str,
    doc_ids: list[str],
    answer: str,
    total_tokens: int = 0,
    escalation_reason: str | None = None,
    escalation_owner: str | None = None,
) -> int:
    """Records a query and, for an unanswered/refused case, a matching
    escalation row - the mock service-desk ticket queue from the plan,
    where questions the assistant couldn't confidently answer get routed
    for a human to review. total_tokens is the sum of what Ollama itself
    reported for this query's embed call plus its generate call (see
    app/graph.py) - real token counts from the model runtime, not an
    estimate. Returns the query's row id."""
    conn = _connect()
    now = datetime.now(timezone.utc).isoformat()
    cur = conn.execute(
        "INSERT INTO queries (timestamp, department, language, question, case_type, doc_ids, answer, total_tokens) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (now, department, language, question, case_type, json.dumps(doc_ids), answer, total_tokens),
    )
    query_id = cur.lastrowid
    if escalation_reason:
        conn.execute(
            "INSERT INTO escalations (query_id, timestamp, reason, owner) VALUES (?, ?, ?, ?)",
            (query_id, now, escalation_reason, escalation_owner),
        )
    conn.commit()
    conn.close()
    return query_id


def department_summary() -> list[dict]:
    """Query volume and unresolved-escalation count per department - the
    view a compliance head would use to spot which policy areas keep
    generating unclear questions."""
    conn = _connect()
    rows = conn.execute("""
        SELECT
            COALESCE(q.department, 'Unknown') AS department,
            COUNT(*) AS total_queries,
            SUM(CASE WHEN e.id IS NOT NULL AND e.resolved = 0 THEN 1 ELSE 0 END) AS open_escalations,
            AVG(q.total_tokens) AS avg_tokens
        FROM queries q
        LEFT JOIN escalations e ON e.query_id = q.id
        GROUP BY department
        ORDER BY total_queries DESC
    """).fetchall()
    conn.close()
    return [
        {
            "department": r[0], "total_queries": r[1], "open_escalations": r[2],
            "avg_tokens": round(r[3]) if r[3] is not None else 0,
        }
        for r in rows
    ]
