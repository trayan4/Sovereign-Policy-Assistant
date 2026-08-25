from typing import TypedDict

import ollama
from langgraph.graph import END, StateGraph

from app.config import CONTRADICTION_MARGIN, GEN_MODEL, OUT_OF_SCOPE_DISTANCE
from app.prompts import (
    FOLLOW_UP_SENTENCE,
    LANGUAGE_DIRECTIVE,
    SYSTEM_CONTRADICTION,
    SYSTEM_EXPIRED,
    SYSTEM_NORMAL,
    SYSTEM_OUT_OF_SCOPE,
    build_user_prompt,
)
from app.query_log import log_query
from app.relationships_db import get_relationship_for_doc
from app.retrieval import detect_language, get_document_chunks, retrieve


class GraphState(TypedDict, total=False):
    question: str
    department: str | None
    clearance: str
    language: str
    retrieved: list[dict]
    case: str
    primary_doc: str | None
    referenced_doc: str | None
    governs_note: str | None
    doc_chunks: dict[str, list[dict]]
    answer: str
    citations: list[dict]
    embed_tokens: int
    total_tokens: int
    excluded_best_distance: float | None


def detect_language_node(state: GraphState) -> GraphState:
    return {"language": detect_language(state["question"])}


def retrieve_node(state: GraphState) -> GraphState:
    clearance = state.get("clearance") or "standard"
    results, embed_tokens, excluded_best_distance = retrieve(
        state["question"], state["language"], clearance
    )
    return {
        "retrieved": results,
        "embed_tokens": embed_tokens,
        "excluded_best_distance": excluded_best_distance,
    }


def assess_node(state: GraphState) -> GraphState:
    """Decides which of the four behaviors applies, using the retrieved
    chunks' own metadata (status) and the relationships database (governance
    overrides) rather than asking the LLM to judge - the LLM only ever
    writes prose from context it's already been told is correct, it never
    decides whether a policy is expired or contradicted.

    Both status and relationships are themselves computed/derived facts
    (see scripts/status_rules.py and scripts/relationships.py), not
    hand-authored - this function just reads them, the same as it always
    did when they came from a JSON file."""
    retrieved = state["retrieved"]
    if not retrieved or retrieved[0]["distance"] > OUT_OF_SCOPE_DISTANCE:
        return {"case": "out_of_scope"}

    excluded_best_distance = state.get("excluded_best_distance")
    if excluded_best_distance is not None and excluded_best_distance < retrieved[0]["distance"]:
        # The single best-matching document for this question was
        # confidential and got excluded (see retrieve()) - and it scored
        # closer than anything we're actually allowed to answer from.
        # Substituting the next-best allowed document here would mean
        # confidently answering from something that isn't really the best
        # match for this question at all - refusing is the honest
        # response, the same one this question would get if no document
        # covered it.
        return {"case": "out_of_scope"}

    by_doc: dict[str, dict] = {}
    for r in retrieved:
        doc_id = r["metadata"]["doc_id"]
        if doc_id not in by_doc or r["distance"] < by_doc[doc_id]["distance"]:
            by_doc[doc_id] = r
    relevant = sorted(
        (d for d in by_doc.values() if d["distance"] <= OUT_OF_SCOPE_DISTANCE),
        key=lambda d: d["distance"],
    )
    best_distance = relevant[0]["distance"]
    # Only a document genuinely competing to answer THIS question - not
    # merely topically nearby - should trigger contradiction handling.
    candidates = [d for d in relevant if d["distance"] <= best_distance + CONTRADICTION_MARGIN]

    for d in candidates:
        doc_id = d["metadata"]["doc_id"]
        rel = get_relationship_for_doc(doc_id)
        if rel:
            return {
                "case": "contradiction",
                "primary_doc": doc_id,
                "referenced_doc": rel["target_doc"],
                "governs_note": rel["evidence"],
            }

    best = relevant[0]
    if best["metadata"]["status"] == "expired":
        return {"case": "expired", "primary_doc": best["metadata"]["doc_id"]}

    return {"case": "normal", "primary_doc": best["metadata"]["doc_id"]}


def gather_context_node(state: GraphState) -> GraphState:
    """Pulls the FULL chunk set (purpose + all clauses) for whichever
    document(s) assess_node identified, rather than just the single
    fragment retrieval happened to match - the model should see the whole
    policy, not one sentence stripped of its surroundings."""
    lang = state["language"]
    clearance = state.get("clearance") or "standard"
    doc_chunks = {}
    if state["case"] == "out_of_scope":
        return {"doc_chunks": {}}

    doc_chunks[state["primary_doc"]] = get_document_chunks(state["primary_doc"], lang, clearance)
    if state["case"] == "contradiction" and state.get("referenced_doc"):
        doc_chunks[state["referenced_doc"]] = get_document_chunks(
            state["referenced_doc"], lang, clearance
        )
    return {"doc_chunks": doc_chunks}


def _citation(meta: dict, governs_note: str = "") -> dict:
    # governs_note is no longer part of chunk metadata (relationships are
    # derived, not stored per-chunk - see app/relationships_db.py); the
    # caller passes it in explicitly for whichever document assess_node
    # identified as the one declaring the override.
    return {
        "doc_id": meta["doc_id"],
        "title": meta["title"],
        "version": meta["version"],
        "effective_date": meta["effective_date"],
        "status": meta["status"],
        "approver_name": meta["approver_name"],
        "approver_role": meta["approver_role"],
        "governs_note": governs_note,
        "confidential": meta.get("classification") == "confidential",
    }


def generate_node(state: GraphState) -> GraphState:
    case = state["case"]
    question = state["question"]
    lang = state["language"]
    doc_chunks = state["doc_chunks"]

    embed_tokens = state.get("embed_tokens") or 0

    if case == "out_of_scope":
        answer, prompt_tokens, output_tokens = _generate(
            SYSTEM_OUT_OF_SCOPE, build_user_prompt(question, [], lang), lang
        )
        total = embed_tokens + prompt_tokens + output_tokens
        return {"answer": answer, "citations": [], "total_tokens": total}

    primary_chunks = doc_chunks[state["primary_doc"]]
    primary_governs_note = state.get("governs_note") or "" if case == "contradiction" else ""
    citations = [_citation(primary_chunks[0]["metadata"], primary_governs_note)]

    if case == "contradiction":
        ref_chunks = doc_chunks.get(state["referenced_doc"], [])
        if ref_chunks:
            citations.append(_citation(ref_chunks[0]["metadata"]))
        blocks = [
            _format_block(primary_chunks, "Policy A"),
            _format_block(ref_chunks, "Policy B") if ref_chunks else "",
        ]
        prompt = build_user_prompt(question, [b for b in blocks if b], lang)
        answer, prompt_tokens, output_tokens = _generate(SYSTEM_CONTRADICTION, prompt, lang)
    elif case == "expired":
        prompt = build_user_prompt(question, [_format_block(primary_chunks, "")], lang)
        answer, prompt_tokens, output_tokens = _generate(SYSTEM_EXPIRED, prompt, lang)
        answer = f"{answer} {FOLLOW_UP_SENTENCE[lang]}"
    else:
        prompt = build_user_prompt(question, [_format_block(primary_chunks, "")], lang)
        answer, prompt_tokens, output_tokens = _generate(SYSTEM_NORMAL, prompt, lang)

    total = embed_tokens + prompt_tokens + output_tokens
    return {"answer": answer, "citations": citations, "total_tokens": total}


def _format_block(chunks: list[dict], label: str) -> str:
    if not chunks:
        return ""
    lines = [f"[{label}]"] if label else []
    for c in chunks:
        lines.append(c["text"])
    return "\n".join(lines)


def _generate(system: str, user: str, language: str) -> tuple[str, int, int]:
    # Reinforced at the system level too, not just in the user turn: for a
    # short/context-light prompt (e.g. out-of-scope, where there's no
    # policy text to ground the answer in) a small model can otherwise
    # default to English regardless of the user turn's own instruction.
    system = f"{system}\n\n{LANGUAGE_DIRECTIVE[language]}"
    resp = ollama.chat(
        model=GEN_MODEL,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        options={"temperature": 0.1},
    )
    text = resp["message"]["content"].strip()
    prompt_tokens = resp.get("prompt_eval_count") or 0
    output_tokens = resp.get("eval_count") or 0
    return text, prompt_tokens, output_tokens


def log_node(state: GraphState) -> GraphState:
    doc_ids = list(state["doc_chunks"].keys())
    escalation_reason = None
    escalation_owner = None
    if state["case"] == "out_of_scope":
        escalation_reason = "Question not covered by policy library"
    elif state["case"] == "expired":
        primary_meta = state["doc_chunks"][state["primary_doc"]][0]["metadata"]
        escalation_reason = f"Only source ({state['primary_doc']}) is expired"
        escalation_owner = primary_meta["approver_name"]

    log_query(
        department=state.get("department"),
        language=state["language"],
        question=state["question"],
        case_type=state["case"],
        doc_ids=doc_ids,
        answer=state["answer"],
        total_tokens=state.get("total_tokens") or 0,
        escalation_reason=escalation_reason,
        escalation_owner=escalation_owner,
    )
    return {}


def build_graph():
    graph = StateGraph(GraphState)
    graph.add_node("detect_language", detect_language_node)
    graph.add_node("retrieve", retrieve_node)
    graph.add_node("assess", assess_node)
    graph.add_node("gather_context", gather_context_node)
    graph.add_node("generate", generate_node)
    graph.add_node("log", log_node)

    graph.set_entry_point("detect_language")
    graph.add_edge("detect_language", "retrieve")
    graph.add_edge("retrieve", "assess")
    graph.add_edge("assess", "gather_context")
    graph.add_edge("gather_context", "generate")
    graph.add_edge("generate", "log")
    graph.add_edge("log", END)

    return graph.compile()


_compiled_graph = None


def get_graph():
    global _compiled_graph
    if _compiled_graph is None:
        _compiled_graph = build_graph()
    return _compiled_graph


def ask(question: str, department: str | None = None, clearance: str = "standard") -> dict:
    # clearance defaults to "standard" (no confidential access) until a real
    # identity provider exists - see app/server.py. Every caller (CLI, API,
    # UI) goes through this same default today, which is the point of
    # Phase 1: prove the filtering itself is correct before anything wires
    # up who's actually allowed to pass "cleared".
    graph = get_graph()
    result = graph.invoke({"question": question, "department": department, "clearance": clearance})
    return {
        "answer": result["answer"],
        "case": result["case"],
        "citations": result["citations"],
        "language": result["language"],
        "total_tokens": result.get("total_tokens") or 0,
    }
