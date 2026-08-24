"""Derives governance relationships between documents from their own
content, replacing the old hand-authored governs_note field entirely.

Stage 1 (self-declared): one LLM call per document, asking whether it
explicitly states it overrides/supersedes another specific policy. High
precision because the document is self-reporting; auto-published, no human
review needed - matches how POL-CASH-002 already states its own override
in plain text.

Stage 2 (undeclared conflicts): for current documents' clauses, find other
current documents' clauses that a similarity search says are on a similar
topic, then ask an LLM whether they actually set conflicting requirements.
Anything flagged goes to a human review queue and is never auto-published -
a wrong "which policy governs" answer has real compliance consequences, so
undeclared conflicts always get a human in the loop before going live."""

import json
import re
from collections import defaultdict

import ollama

from app.relationships_db import add_pending_relationship, add_relationship

GEN_MODEL = "gemma2:2b"
DOC_ID_RE = re.compile(r"POL-[A-Z]+-\d+")

# Tighter than the general retrieval relevance threshold (config.py's
# OUT_OF_SCOPE_DISTANCE): here we specifically want clauses similar enough
# that a real conflict is plausible, not merely the same broad topic area.
CONFLICT_SCAN_DISTANCE = 0.35

STAGE1_PROMPT = """You are analyzing an internal policy document for governance metadata. \
Read the clauses below. Does this document explicitly state that it overrides, supersedes, \
or takes precedence over another specific policy (referenced by an ID like "POL-XXX-NNN")? \
Only answer yes if a specific other policy ID is named in the text - do not guess. \
Respond with ONLY a JSON object, no other text: \
{"declares_override": true or false, "target_doc_id": "POL-XXX-NNN or null", "evidence": "the exact sentence, or null"}"""

STAGE2_PROMPT = """You are comparing two policy clauses from different documents that a \
similarity search flagged as covering a similar topic. Read both. Do they set DIFFERENT or \
CONFLICTING requirements for what appears to be the same real-world scenario (e.g. different \
numeric limits, different conditions, different approval thresholds)? If they are about \
different topics, or state compatible/complementary rules, answer false. \
Respond with ONLY a JSON object, no other text: \
{"conflicting": true or false, "reasoning": "one sentence"}"""


def _chat_json(system: str, user: str) -> dict:
    resp = ollama.chat(
        model=GEN_MODEL,
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        format="json",
        options={"temperature": 0.0},
    )
    try:
        return json.loads(resp["message"]["content"])
    except (json.JSONDecodeError, KeyError):
        return {}



# Belt-and-suspenders check on top of the LLM's own judgment: observed in
# testing that a small model can call "disciplinary offence under POL-X"
# (a plain cross-reference) a declared override. A self-declared relationship
# auto-publishes with no human review, on the assumption the document is
# unambiguously stating it itself - that assumption only holds if the
# evidence text actually contains override language, so this is required
# in addition to the model's own true/false judgment, not instead of it.
OVERRIDE_LANGUAGE_RE = re.compile(
    r"supersed|overrid|takes? precedence|governs?\b", re.IGNORECASE
)


def detect_self_declared_relationship(doc_id: str, en_clause_texts: list[str]) -> dict | None:
    """Stage 1 for one document. Writes directly to the live relationships
    table if found (self-declared = trusted), returns it for logging."""
    body = "\n".join(en_clause_texts)
    result = _chat_json(STAGE1_PROMPT, body)
    if not result.get("declares_override"):
        return None
    target = result.get("target_doc_id")
    if not target or not DOC_ID_RE.fullmatch(target):
        return None
    evidence = result.get("evidence") or ""
    if not OVERRIDE_LANGUAGE_RE.search(evidence):
        return None
    add_relationship(doc_id, target, "overrides", evidence, origin="self_declared")
    return {"source_doc": doc_id, "target_doc": target, "evidence": evidence}


def compare_clause_pair(doc_a: str, text_a: str, doc_b: str, text_b: str, distance: float) -> bool:
    """Stage 2 for one candidate pair. Writes to the pending review queue if
    judged conflicting - never auto-publishes. Returns whether it flagged."""
    user = f"Clause from {doc_a}:\n{text_a}\n\nClause from {doc_b}:\n{text_b}"
    result = _chat_json(STAGE2_PROMPT, user)
    if not result.get("conflicting"):
        return False
    evidence = (
        f"{doc_a}: {text_a}\n---\n{doc_b}: {text_b}\n---\n"
        f"Reasoning: {result.get('reasoning', '')}"
    )
    add_pending_relationship(doc_a, doc_b, evidence, confidence=round(1 - distance, 3))
    return True


def scan_for_undeclared_conflicts(collection, declared_pairs: set[tuple[str, str]]) -> int:
    """Orchestrates Stage 2 across the whole (already-embedded) collection.
    declared_pairs: doc_id pairs already covered by a Stage 1 relationship,
    in either order - skipped here since a self-declared relationship is
    already the trusted answer for that pair. Reuses each clause's embedding
    already stored in Chroma rather than re-embedding via Ollama, since the
    vectors already exist. Returns how many pairs were flagged for review."""
    all_current_en_clauses = collection.get(
        where={"$and": [{"language": "en"}, {"chunk_type": "clause"}, {"status": "current"}]},
        include=["documents", "metadatas", "embeddings"],
    )
    by_doc: dict[str, list[dict]] = defaultdict(list)
    for id_, doc, meta, emb in zip(
        all_current_en_clauses["ids"],
        all_current_en_clauses["documents"],
        all_current_en_clauses["metadatas"],
        all_current_en_clauses["embeddings"],
    ):
        by_doc[meta["doc_id"]].append({"id": id_, "text": doc, "meta": meta, "embedding": emb})

    checked_pairs: set[tuple[str, str]] = set()
    flagged_count = 0

    for doc_id, chunks in by_doc.items():
        for chunk in chunks:
            results = collection.query(
                query_embeddings=[chunk["embedding"]],
                n_results=5,
                where={"$and": [{"language": "en"}, {"chunk_type": "clause"}, {"status": "current"}]},
            )
            for other_id, other_text, other_meta, dist in zip(
                results["ids"][0], results["documents"][0], results["metadatas"][0], results["distances"][0]
            ):
                other_doc_id = other_meta["doc_id"]
                if other_doc_id == doc_id or dist > CONFLICT_SCAN_DISTANCE:
                    continue
                pair_key = tuple(sorted([doc_id, other_doc_id]))
                if pair_key in checked_pairs or pair_key in declared_pairs:
                    continue
                checked_pairs.add(pair_key)
                if compare_clause_pair(doc_id, chunk["text"], other_doc_id, other_text, dist):
                    flagged_count += 1

    return flagged_count
