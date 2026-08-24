#!/usr/bin/env python3
"""FastAPI wrapper exposing the LangGraph pipeline as a service - the
interface a front end (or the demo UI) talks to, and what the Docker
container runs."""

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

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


class AskResponse(BaseModel):
    answer: str
    case: str
    language: str
    citations: list[Citation]


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/ask", response_model=AskResponse)
def ask_endpoint(req: AskRequest):
    result = ask(req.question, department=req.department)
    return result


@app.get("/departments")
def departments():
    """Query volume and open-escalation count per department - what a
    compliance head would look at to spot unclear policy areas."""
    return department_summary()


class ReviewRequest(BaseModel):
    approve: bool
    note: str | None = None


@app.get("/admin/pending-relationships")
def pending_relationships():
    """Candidate conflicts Stage 2 of ingestion flagged between two current
    policies that don't explicitly declare a relationship themselves - each
    needs a human decision before it can affect what the assistant tells
    staff. Never auto-published; see scripts/relationships.py."""
    return list_pending()


@app.post("/admin/pending-relationships/{pending_id}/review")
def review_pending_relationship(pending_id: int, req: ReviewRequest):
    """Approving promotes the candidate into the live relationships table
    (assess_node starts using it on the next request); rejecting just
    records the decision. Either way leaves an audit trail."""
    try:
        review_pending(pending_id, approve=req.approve, note=req.note)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return {"id": pending_id, "status": "approved" if req.approve else "rejected"}
