#!/usr/bin/env python3
"""FastAPI wrapper exposing the LangGraph pipeline as a service - the
interface a front end (or the demo UI) talks to, and what the Docker
container runs."""

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel

from app.auth import get_admin, get_clearance, get_username
from app.graph import ask
from app.query_log import department_summary
from app.relationships_db import list_pending, review_pending
from app.service_requests import list_open as list_open_srs
from app.service_requests import raise_sr
from app.service_requests import resolve as resolve_sr

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


class RaiseSRRequest(BaseModel):
    question: str
    case_type: str
    urgency: str
    note: str | None = None


class RaiseSRResponse(BaseModel):
    reference: str


@app.post("/follow-up/sr", response_model=RaiseSRResponse)
def raise_sr_endpoint(req: RaiseSRRequest, username: str = Depends(get_username)):
    """Raised directly from a refused answer in the UI - the active
    follow-up path, distinct from the automatic silent logging every
    out_of_scope/expired case already gets in log_node (see
    app/graph.py). Any authenticated account can raise one (get_username,
    not get_admin) - the requester is taken from the validated token, not
    from anything the client could type into the request body."""
    reference = raise_sr(
        question=req.question,
        case_type=req.case_type,
        urgency=req.urgency,
        note=req.note,
        requester=username,
    )
    return {"reference": reference}


@app.get("/departments")
def departments(_admin: str = Depends(get_admin)):
    """Query volume and open-escalation count per department - what a
    compliance head would look at to spot unclear policy areas.

    Gated on the compliance_admin role specifically, not just any login -
    this is a different kind of access than cleared_staff (who's allowed
    to see confidential POLICY CONTENT) entirely, so it gets its own
    dependency (get_admin) rather than reusing get_clearance."""
    return department_summary()


class ReviewRequest(BaseModel):
    approve: bool
    note: str | None = None


@app.get("/admin/pending-relationships")
def pending_relationships(_admin: str = Depends(get_admin)):
    """Candidate conflicts Stage 2 of ingestion flagged between two current
    policies that don't explicitly declare a relationship themselves - each
    needs a human decision before it can affect what the assistant tells
    staff. Never auto-published; see scripts/relationships.py."""
    return list_pending()


@app.post("/admin/pending-relationships/{pending_id}/review")
def review_pending_relationship(
    pending_id: int, req: ReviewRequest, _admin: str = Depends(get_admin)
):
    """Approving promotes the candidate into the live relationships table
    (assess_node starts using it on the next request); rejecting just
    records the decision. Either way leaves an audit trail.

    This is the highest-stakes of the admin endpoints - it's a write that
    changes what the assistant tells every future user, not just a read -
    so this one specifically needed the real role check, not just "any
    login," from the start."""
    try:
        review_pending(pending_id, approve=req.approve, note=req.note)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return {"id": pending_id, "status": "approved" if req.approve else "rejected"}


@app.get("/admin/service-requests")
def service_requests(_admin: str = Depends(get_admin)):
    """Open SRs raised via /follow-up/sr - the active follow-up queue a
    compliance admin actually works through, as opposed to department
    summary's aggregate escalation count that never told you what was
    actually in it."""
    return list_open_srs()


@app.post("/admin/service-requests/{sr_id}/resolve")
def resolve_service_request(sr_id: int, _admin: str = Depends(get_admin)):
    try:
        resolve_sr(sr_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return {"id": sr_id, "status": "resolved"}
