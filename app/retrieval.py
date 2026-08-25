import re

import chromadb
import ollama

from app.config import CHROMA_PATH, COLLECTION_NAME, EMBED_MODEL, RETRIEVE_N

ARABIC_RANGE_RE = re.compile(r"[؀-ۿ]")
LATIN_RE = re.compile(r"[A-Za-z]")

_client = None


def get_collection():
    """Resolves the collection by name fresh on every call - deliberately
    not cached. scripts/ingest.py deletes and recreates the collection on
    every run (each re-index gets a new internal collection id), which the
    folder watcher (scripts/watch_folder.py) can trigger at any time while
    this service keeps running. Caching the collection OBJECT (rather than
    just the client) was tried and confirmed broken: the long-running app
    process kept its stale reference to the deleted collection and every
    request failed with chromadb.errors.NotFoundError until it was
    restarted - exactly defeating the point of an automatic re-index. The
    client itself is cheap to keep around; re-resolving the collection by
    name is a lightweight metadata lookup, not a data reload, so doing it
    per-request costs nothing meaningful."""
    global _client
    if _client is None:
        _client = chromadb.PersistentClient(path=str(CHROMA_PATH))
    return _client.get_collection(COLLECTION_NAME)


def detect_language(text: str) -> str:
    """Same content-based detection used at ingestion time: classify by
    actual Unicode content, not by an assumed input format."""
    arabic_count = len(ARABIC_RANGE_RE.findall(text))
    latin_count = len(LATIN_RE.findall(text))
    return "ar" if arabic_count > latin_count else "en"


def retrieve(
    question: str, language: str, clearance: str = "standard", n: int = RETRIEVE_N
) -> tuple[list[dict], int, float | None]:
    """Embeds the question and returns the top-n closest chunks, each as
    {id, text, metadata, distance}, ordered by relevance (ascending
    distance), plus the token count Ollama reports for embedding the
    question. Deliberately NOT filtered by language: bge-m3 is a
    multilingual embedding model, so a question and its relevant policy
    text land close together in the same vector space regardless of which
    language either one is written in - not every document in this
    library carries a translation in both languages, so hard-filtering by
    language would make an Arabic question blind to an English-only
    document (and vice versa) even when it's the only source that answers
    it. `language` is passed through only because the caller (assess_node)
    still needs it to know which language to answer IN, not to restrict
    what counts as a match.

    `clearance` DOES restrict what counts as a match: unless it's
    "cleared", chunks whose document declared itself
    `Classification: Confidential` (see metadata_extraction.py) are
    excluded from candidacy entirely, at the database query level - not
    filtered out of the results afterward. That distinction matters: a
    standard-clearance question that only a confidential document could
    answer should behave exactly like an out-of-scope question (nothing
    relevant found), not like a "found it, but redacted" response that
    confirms a confidential policy exists on that topic.

    Also returns excluded_best_distance: when a confidential document was
    excluded, how close its single best-matching chunk scored - or None
    if clearance is "cleared" (nothing was excluded) or nothing
    confidential matched at all. This exists so assess_node can tell "the
    true best answer was confidential, so refuse" apart from "nothing
    confidential was even close, so it's fine to answer from whatever
    scored best among what's allowed" - without that check, filtering out
    the one document that actually answers a question just makes the
    system confidently substitute a *different, wrong* document instead,
    which is worse than refusing. The confidential chunk's own TEXT never
    enters this - only its distance (include=["distances"], no
    "documents") - so a standard-clearance request never holds confidential
    content in memory even transiently."""
    resp = ollama.embed(model=EMBED_MODEL, input=[question])
    embed_tokens = resp.get("prompt_eval_count") or 0
    collection = get_collection()
    query_kwargs = {"query_embeddings": resp["embeddings"], "n_results": n}
    excluded_best_distance = None
    if clearance != "cleared":
        query_kwargs["where"] = {"classification": {"$ne": "confidential"}}
        confidential_check = collection.query(
            query_embeddings=resp["embeddings"],
            n_results=1,
            where={"classification": "confidential"},
            include=["distances"],
        )
        if confidential_check["ids"][0]:
            excluded_best_distance = confidential_check["distances"][0][0]
    res = collection.query(**query_kwargs)
    results = []
    for id_, doc, meta, dist in zip(
        res["ids"][0], res["documents"][0], res["metadatas"][0], res["distances"][0]
    ):
        results.append({"id": id_, "text": doc, "metadata": meta, "distance": dist})
    return results, embed_tokens, excluded_best_distance


def get_document_chunks(doc_id: str, language: str, clearance: str = "standard") -> list[dict]:
    """All chunks for a specific document, preferring the requested
    language but falling back to whatever language the document actually
    has if it doesn't carry one in `language` - now a real case, not a
    hypothetical: a document can be English-only or Arabic-only, so a
    question in the other language can still legitimately match it (see
    retrieve()). The caller (generate_node) already instructs the model to
    answer in the question's language regardless of what language the
    source text is in. Ordered by clause_no (purpose/context first). Used
    once a target document is identified, to pull its full content rather
    than just the matched fragment.

    `clearance` is a second, independent enforcement of the same rule
    retrieve() applies - defense in depth, not redundant: retrieve()
    already keeps a confidential document from ever becoming the assessed
    match for a standard-clearance question, but this function guards
    against a confidential doc_id reaching it through any other path
    (e.g. the contradiction case's referenced_doc) from ever actually
    returning that document's text."""
    collection = get_collection()
    doc_filter = {"$and": [{"doc_id": doc_id}, {"language": language}]}
    if clearance != "cleared":
        doc_filter = {"$and": [*doc_filter["$and"], {"classification": {"$ne": "confidential"}}]}
    res = collection.get(where=doc_filter)
    if not res["ids"]:
        fallback_filter = {"doc_id": doc_id}
        if clearance != "cleared":
            fallback_filter = {"$and": [fallback_filter, {"classification": {"$ne": "confidential"}}]}
        res = collection.get(where=fallback_filter)
    chunks = [
        {"id": id_, "text": doc, "metadata": meta}
        for id_, doc, meta in zip(res["ids"], res["documents"], res["metadatas"])
    ]
    chunks.sort(key=lambda c: c["metadata"]["clause_no"])
    return chunks
