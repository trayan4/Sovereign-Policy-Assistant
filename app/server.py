#!/usr/bin/env python3
"""FastAPI wrapper exposing the LangGraph pipeline as a service - the
interface a front end (or the demo UI) talks to, and what the Docker
container runs."""

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel

from app.auth import get_clearance
from app.graph import ask
from app.query_log import department_summary
from app.relationships_db import list_pending, review_pending

app = FastAPI(title="Sovereign Policy Assistant")


class AskRequest(BaseModel):
    question: str
    department: str | None = None


class Citation(BaseModel):
    doc_id: str
    title: str
    version: str
    effective_date: str
    status: str
    approver_name: str
    approver_role: str
    governs_note: str
    confidential: bool


class AskResponse(BaseModel):
    answer: str
    case: str
    language: str
    citations: list[Citation]
    total_tokens: int


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/ask", response_model=AskResponse)
def ask_endpoint(req: AskRequest, clearance: str = Depends(get_clearance)):
    # clearance is never read from req - see app/auth.py. A valid,
    # Keycloak-signed token is required to reach this endpoint at all;
    # get_clearance() raises 401 before this line runs if one wasn't
    # presented, so there's no unauthenticated fallback to "standard" here.
    result = ask(req.question, department=req.department, clearance=clearance)
    return result


@app.get("/departments")
def departments(_clearance: str = Depends(get_clearance)):
    """Query volume and open-escalation count per department - what a
    compliance head would look at to spot unclear policy areas.

    Gated on being logged in at all (any valid token), not on a specific
    role - there's no distinct "admin" role yet to check instead. That's
    a real gap, not a final design: this stops an anonymous caller from
    reading it, but doesn't yet stop a standard_staff account from doing
    so either. Narrowing this to an actual compliance-admin role is a
    follow-up, not done here."""
    return department_summary()


class ReviewRequest(BaseModel):
    approve: bool
    note: str | None = None


@app.get("/admin/pending-relationships")
def pending_relationships(_clearance: str = Depends(get_clearance)):
    """Candidate conflicts Stage 2 of ingestion flagged between two current
    policies that don't explicitly declare a relationship themselves - each
    needs a human decision before it can affect what the assistant tells
    staff. Never auto-published; see scripts/relationships.py.

    Same caveat as departments() above: requires login, not yet a specific
    admin role."""
    return list_pending()


@app.post("/admin/pending-relationships/{pending_id}/review")
def review_pending_relationship(
    pending_id: int, req: ReviewRequest, _clearance: str = Depends(get_clearance)
):
    """Approving promotes the candidate into the live relationships table
    (assess_node starts using it on the next request); rejecting just
    records the decision. Either way leaves an audit trail.

    This is the highest-stakes of the three newly-gated endpoints - it's a
    write that changes what the assistant tells every future user, not
    just a read - so requiring login here matters even before a proper
    admin role exists to narrow it further."""
    try:
        review_pending(pending_id, approve=req.approve, note=req.note)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return {"id": pending_id, "status": "approved" if req.approve else "rejected"}
