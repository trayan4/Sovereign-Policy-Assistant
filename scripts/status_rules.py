"""Computes each document's status from dates and version relationships -
never stored, never hand-set. A document is "current" only if it's already
in effect, hasn't passed its own expiry (if it has one), and isn't an older
version of a document that's since been reissued.

This is deliberately a pure function over the whole ingested set, run once
per ingestion (or on a schedule in production, since a document with no new
version can still silently expire on the calendar with nothing else
changing)."""

from datetime import date, datetime


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None


def _parse_version(value: str) -> tuple:
    """Sorts '1.2' before '1.10' correctly (not lexicographically), and
    falls back gracefully if a version isn't numeric-dotted."""
    parts = []
    for p in value.split("."):
        try:
            parts.append(int(p))
        except ValueError:
            parts.append(0)
    return tuple(parts)


def compute_statuses(documents: list[dict], today: date | None = None) -> None:
    """Mutates each document dict in place, adding 'status' ("current" or
    "expired") and 'superseded_by' (doc_id of the newer version, or None).

    documents: list of metadata dicts, each with at least doc_id, version,
    effective_date, and optionally expiry_date.

    Supersession groups documents by doc_id - if a real deployment reissues
    a policy as a new PDF sharing the same Document ID with a later
    effective_date, the earlier one is automatically superseded. In this
    project's current corpus every doc_id is unique (one PDF each), so this
    is a no-op here, but the mechanism is what makes a re-issued policy
    handle itself correctly without anyone updating a flag by hand."""
    today = today or date.today()

    by_doc_id: dict[str, list[dict]] = {}
    for d in documents:
        by_doc_id.setdefault(d["doc_id"], []).append(d)

    for doc_id, versions in by_doc_id.items():
        versions.sort(
            key=lambda d: (_parse_version(d["version"]), _parse_date(d["effective_date"])),
            reverse=True,
        )
        latest = versions[0]
        for d in versions:
            d["superseded_by"] = None if d is latest else f"{latest['doc_id']} v{latest['version']}"

    for d in documents:
        effective = _parse_date(d.get("effective_date"))
        expiry = _parse_date(d.get("expiry_date"))

        not_yet_effective = effective is not None and effective > today
        past_expiry = expiry is not None and expiry < today
        superseded = d.get("superseded_by") is not None

        # "expired" here also covers "not yet effective": both mean the
        # document isn't currently in force, which is the one distinction
        # the rest of the system (the refusal behavior in app/graph.py)
        # actually acts on.
        d["status"] = "expired" if (not_yet_effective or past_expiry or superseded) else "current"
