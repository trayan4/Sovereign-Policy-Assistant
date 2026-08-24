#!/usr/bin/env python3
"""Ingestion pipeline: Docling parses policy_library/*.pdf, chunks are built
from Docling's own structured document model, metadata (doc_id, version,
dates, approver) is extracted from each PDF's own metadata block rather than
a separately maintained index file, status is computed from dates rather
than stored, governance relationships are derived from document content
rather than hand-authored, chunks are embedded via Ollama's bge-m3, and the
whole set is loaded into a local persistent Chroma collection.

Chunking is driven by Docling's structural item types and by the document's
own content (language, numbered-list markers) - not by any header text
specific to this corpus. That keeps the pipeline usable on a differently
formatted PDF: Docling already distinguishes intro-style prose ("text") from
enumerated points ("list_item"), and language is detected per item from its
actual Unicode content rather than an assumed section label."""

import re
import sys
from collections import defaultdict
from pathlib import Path

import chromadb
import ollama
import pymupdf
from docling.document_converter import DocumentConverter, PdfFormatOption
from docling.datamodel.base_models import InputFormat
from docling.datamodel.pipeline_options import (
    AcceleratorDevice,
    AcceleratorOptions,
    PdfPipelineOptions,
)

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))  # so `app.*` imports resolve regardless of how this script is invoked

from metadata_extraction import extract_metadata_fields, validate_metadata  # noqa: E402
from status_rules import compute_statuses  # noqa: E402
from relationships import detect_self_declared_relationship, scan_for_undeclared_conflicts  # noqa: E402
from app.relationships_db import RELATIONSHIPS_DB_PATH  # noqa: E402

POLICY_DIR = ROOT / "policy_library"
CHROMA_PATH = ROOT / "chroma_store"
EMBED_MODEL = "bge-m3"
COLLECTION_NAME = "policy_library"

# A generic numbered-list marker ("1. ", "2. ", ...) - a broadly common
# document convention, not specific to this corpus. Used only as a fallback
# to split an item's text when Docling's own layout detection merged several
# enumerated points into one block instead of separate list_items. Anchored
# to a word boundary on the left (start-of-string or preceding whitespace)
# so it can't match a numeric tail embedded in a larger token, such as the
# "001" in a cross-reference like "POL-KYC-001." - without the anchor, that
# digit run followed by "." and a space looks identical to a genuine list
# marker and gets split there, severing the reference mid-token.
CLAUSE_SPLIT_RE = re.compile(r"(?:^|(?<=\s))(\d+)\.\s+")
ARABIC_RANGE_RE = re.compile(r"[؀-ۿ]")
LATIN_RE = re.compile(r"[A-Za-z]")

# Labels that are structural furniture or non-prose, never body content.
# Deliberately a blocklist, not an allowlist of "text"/"list_item": Docling's
# layout classifier can mislabel prose as other types (observed: some of this
# corpus's Arabic paragraphs were classified as "code", likely due to font/
# spacing characteristics of the source PDF's Arabic rendering) - excluding
# only clearly non-prose labels is far more robust to that than assuming
# content only ever arrives as "text" or "list_item".
NON_CONTENT_LABELS = {
    "section_header", "title", "page_header", "page_footer", "table",
    "picture", "chart", "formula", "document_index", "checkbox_selected",
    "checkbox_unselected", "form", "key_value_region", "field_region",
    "field_heading", "field_item", "field_key", "field_value", "field_hint",
    "grading_scale", "empty_value", "marker", "caption",
}


def detect_language(text: str) -> str:
    """Classifies a text block by its actual Unicode content, not by a
    section label - works regardless of how (or whether) a document marks
    its own language sections."""
    arabic_count = len(ARABIC_RANGE_RE.findall(text))
    latin_count = len(LATIN_RE.findall(text))
    return "ar" if arabic_count > latin_count else "en"


LATIN_RUN_RE = re.compile(r"[A-Za-z][A-Za-z0-9\-]*")


LIST_MARKER_RE = re.compile(r"\d+\.")


def _is_latin_pivot(token: str) -> bool:
    """True for a token that keeps its own fixed position rather than
    flowing with the surrounding Arabic text's reversal: a Latin-script
    run (a cross-reference like "(POL-X-001)" or an acronym like "BYOD"),
    or a bare numbered-list marker ("1.", "2.", ...), which - unlike an
    ordinary number such as a currency amount - is rendered as its own
    fixed-position bullet glyph rather than flowing RTL text. A currency-
    style number extracts fine as part of the surrounding Arabic run and
    must NOT be treated as a pivot, which is why this only matches a
    number immediately followed by a period, not any digit sequence."""
    if LIST_MARKER_RE.fullmatch(token):
        return True
    core = token.strip("().,;:؛،")
    return bool(core) and bool(LATIN_RUN_RE.fullmatch(core))


SENTENCE_END = {".", ",", "،", "؛", "؟", "!"}


def _reverse_tokens_with_pivots(tokens: list[str]) -> list[str]:
    """Core reordering step, operating on an already-tokenized line: split
    on Latin-script pivot tokens (a cross-reference like "(POL-X-001)" or
    an acronym like "BYOD") and reverse only the Arabic segments between
    them, leaving pivots in place. A line with no pivot is just one segment,
    so this is a strict generalization of a plain whole-line reversal -
    verified empirically against known-correct source text, not assumed:
    a naive whole-line reversal is provably wrong whenever a Latin run is
    embedded, because PDF text-layer extraction keeps that run's own
    internal order while independently reversing the Arabic on each side
    of it, not the line as a single unit."""
    segments = []  # list of ("rtl", [tokens]) | ("pivot", [token])
    current = []
    for tok in tokens:
        if _is_latin_pivot(tok):
            if current:
                segments.append(("rtl", current))
                current = []
            segments.append(("pivot", [tok]))
        else:
            current.append(tok)
    if current:
        segments.append(("rtl", current))

    out = []
    for kind, seg in segments:
        out.extend(reversed(seg) if kind == "rtl" else seg)
    return out


def _relocate_stray_sentence_end(tokens: list[str]) -> str:
    """A sentence-final mark can land anywhere but the true end after
    segment-wise reversal (it belonged to whichever segment used to be
    last, before that segment's own reversal moved it off the end).
    Anywhere but the true end is wrong for a sentence-final mark, so pull
    every stray occurrence out and append it once, in order, at the end."""
    tokens = list(tokens)
    trailing = ""
    i = 0
    while i < len(tokens):
        if tokens[i] in SENTENCE_END and i != len(tokens) - 1:
            trailing += tokens.pop(i)
        else:
            i += 1
    result = " ".join(tokens)
    return result + trailing if trailing else result


def fix_arabic_word_order(text: str) -> str:
    """String-based fallback path (used when a per-line geometric
    reconstruction via PyMuPDF isn't available, e.g. missing bounding-box
    data): applies the same pivot-aware reversal to the whole text as one
    line. Correct for genuinely single-line content; a clause that wraps
    across multiple PDF lines needs reconstruct_arabic_text_via_geometry
    instead, since Docling's flattened .text loses the line boundaries
    that a wrapped clause needs reversed independently per line."""
    tokens = text.split()
    if not tokens:
        return text
    return _relocate_stray_sentence_end(_reverse_tokens_with_pivots(tokens))


def get_pymupdf_lines_for_item(pdf_page_for, item) -> list[list[str]]:
    """Returns each visual line's words (in on-page left-to-right stream
    order) within a Docling item's bounding box(es), using PyMuPDF's word-
    level extraction. This preserves true line boundaries that Docling's
    flattened .text loses when it merges wrapped or multi-point content
    into a single block - confirmed directly against this corpus's PDFs:
    Docling and raw PyMuPDF both extract the same (visually-ordered, not
    reading-ordered) text, but PyMuPDF's per-word bounding boxes are intact,
    so grouping by (block, line) and sorting each group by x-position
    recovers the line structure Docling's text string lost."""
    lines_by_pos = []
    for prov in getattr(item, "prov", []):
        page, page_h = pdf_page_for(prov.page_no)
        if page is None:
            continue
        bbox = prov.bbox
        # Docling's bbox can be a tight bound around the text, tight enough
        # that a narrow trailing glyph (a sentence-final period) sits right
        # at the edge and gets excluded by an exact clip - a small margin
        # avoids that boundary effect without risking pulling in a
        # neighboring item's text (PDF line spacing is comfortably larger
        # than a few points).
        margin = 2
        rect = pymupdf.Rect(
            bbox.l - margin, page_h - bbox.t - margin,
            bbox.r + margin, page_h - bbox.b + margin,
        )
        words = page.get_text("words", clip=rect)
        grouped = defaultdict(list)
        for w in words:
            grouped[(w[5], w[6])].append(w)
        for ws in grouped.values():
            y0 = min(w[1] for w in ws)
            ordered = [w[4] for w in sorted(ws, key=lambda w: w[7])]
            lines_by_pos.append((y0, ordered))

    lines_by_pos.sort(key=lambda x: x[0])
    # A leading visual list-marker glyph (e.g. "1.") picked up because it
    # falls inside the clipped region is deliberately left in place rather
    # than stripped here: the caller's CLAUSE_SPLIT_RE already handles it
    # correctly either way - for an item Docling tracks positionally
    # (list_item) it produces an empty, filtered-out prefix ahead of the
    # (redundantly) explicit clause number, and for an item Docling does
    # NOT track positionally (observed: some of this corpus's Arabic
    # clauses arrive labeled "code" rather than "list_item"), that marker
    # is the ONLY signal identifying which clause this is - stripping it
    # there would silently discard the clause's identity.
    return [tokens for _, tokens in lines_by_pos]


def reconstruct_arabic_text_via_geometry(pdf_page_for, item) -> str | None:
    """Full per-item reconstruction: get true visual lines via PyMuPDF,
    reverse each independently (pivot-aware), and concatenate top-to-bottom.
    Returns None if no bounding-box data is available, so the caller can
    fall back to the string-based fix_arabic_word_order.

    Deliberately does NOT relocate stray sentence-final punctuation here:
    when an item merges several clauses (Docling's layout misdetection),
    doing that across the whole merged block would let a later clause's
    leading stray period get pulled loose and dumped at the very end of
    the WHOLE item, past every other clause - each clause needs its own
    period kept with its own text. The caller relocates per-clause, after
    splitting on the clause-number markers this function preserves."""
    lines = get_pymupdf_lines_for_item(pdf_page_for, item)
    if not lines:
        return None
    all_tokens = []
    for line_tokens in lines:
        all_tokens.extend(_reverse_tokens_with_pivots(line_tokens))
    if not all_tokens:
        return None
    return " ".join(all_tokens)


def strip_label_prefix(text: str) -> str:
    """Strips a short leading 'Label:' prefix (e.g. an author-written
    'Purpose:' lead-in), iteratively, since a merged block can carry more
    than one stray label-like fragment. Only strips short (<=3 word)
    candidates so it can't eat into a genuine sentence that happens to
    contain an early colon."""
    while True:
        idx = text.find(":")
        if 0 <= idx <= 20:
            candidate = text[:idx].strip()
            if candidate == "" or len(candidate.split()) <= 3:
                text = text[idx + 1:].strip()
                continue
        break
    return text


def chunk_document_body(doc, pdf_page_for=None) -> list[dict]:
    """Walks Docling's structured items in document order. Skips headings
    and a leading metadata-labeled block (detected generically by the word
    'metadata' in a heading - a common convention, not a fixed schema).
    Every remaining list_item/text item becomes one or more chunks:
      - "text" items (Docling's own classification for intro-style prose)
        are treated as context chunks.
      - "list_item" items (Docling's own classification for enumerated
        points) are treated as clause chunks, numbered by their position
        in the current run - UNLESS the item's own text contains multiple
        embedded "N. " markers, meaning Docling merged several points into
        one block, in which case it is split on those markers instead.

    For an item Docling classifies as Arabic-dominant, its text is first
    replaced with a geometrically reconstructed version (see
    reconstruct_arabic_text_via_geometry) when page/bbox data is available,
    since that correctly handles a clause wrapping across multiple PDF
    lines - something no amount of re-splitting Docling's already-flattened
    text can recover, because the line boundary itself is gone by then."""
    chunks = []
    in_metadata = False
    clause_counter = 0

    for item, _level in doc.iterate_items():
        label = getattr(item, "label", "")
        text = getattr(item, "text", "").strip()

        if label == "section_header":
            in_metadata = "metadata" in text.lower()
            clause_counter = 0
            continue
        if label in NON_CONTENT_LABELS or not text or in_metadata:
            continue

        already_reordered = False
        if pdf_page_for is not None and detect_language(text) == "ar":
            reconstructed = reconstruct_arabic_text_via_geometry(pdf_page_for, item)
            if reconstructed:
                text = reconstructed
                already_reordered = True

        parts = CLAUSE_SPLIT_RE.split(text)
        was_split = len(parts) > 1
        if not was_split:
            segments = [(None, text)]
        else:
            # The prefix before the first explicit number is intro-like
            # content merged into this block by imperfect layout detection -
            # it is NOT itself a numbered point, regardless of the parent
            # item's own label.
            segments = [(None, parts[0])] + [
                (int(parts[i]), parts[i + 1]) for i in range(1, len(parts) - 1, 2)
            ]

        for explicit_no, raw_seg in segments:
            lang = detect_language(raw_seg)
            # Text already reconstructed via geometry is already correctly
            # ordered; the string-based reversal is only for the fallback
            # path (no bbox data, or reconstruction failed for this item)
            # where raw_seg is still visually-ordered.
            if lang == "ar" and not already_reordered:
                seg = fix_arabic_word_order(raw_seg)
            elif lang == "ar" and already_reordered:
                # Already reversed at the geometry-reconstruction stage;
                # only the per-clause punctuation relocation (deliberately
                # deferred until after this split) still needs doing.
                seg = _relocate_stray_sentence_end(raw_seg.split())
            else:
                seg = raw_seg
            seg = strip_label_prefix(" ".join(seg.split()).strip())
            if not seg or not any(ch.isalnum() for ch in seg):
                continue

            if explicit_no is not None:
                clause_no = explicit_no
                clause_counter = max(clause_counter, clause_no)
                chunk_type = "clause"
            elif was_split:
                clause_no = 0
                chunk_type = "context"
            elif label == "list_item":
                clause_counter += 1
                clause_no = clause_counter
                chunk_type = "clause"
            else:
                clause_no = 0
                chunk_type = "context"

            chunks.append({
                "language": lang,
                "chunk_type": chunk_type,
                "clause_no": clause_no,
                "text": seg,
            })

    return _drop_duplicate_chunks(chunks)


def _drop_duplicate_chunks(chunks: list[dict]) -> list[dict]:
    """Docling occasionally emits an overlapping duplicate item for the same
    on-page content (observed on a small number of this corpus's PDFs, not
    specific to any one document) - sometimes an exact repeat, sometimes a
    truncated partial repeat alongside the complete version. Guarding
    against this generically (any two same-language chunks where one text
    is contained in the other) is more robust than special-casing whichever
    documents happen to trigger it, since any real document set could hit
    the same layout-detection quirk. The shorter/truncated one is dropped,
    keeping the more complete text."""
    normalized = [c["text"].strip(" .,؛،:!؟") for c in chunks]
    keep = []
    for i, c in enumerate(chunks):
        dominated = False
        for j, other in enumerate(chunks):
            if i == j or c["language"] != other["language"]:
                continue
            if normalized[i] == normalized[j]:
                if j < i:
                    dominated = True
                    break
                continue
            if normalized[i] and normalized[i] in normalized[j]:
                dominated = True
                break
        if not dominated:
            keep.append(c)
    return keep


def make_pdf_page_lookup(pdf_path: str):
    """Returns a (page_no) -> (page, page_height) callback backed by
    PyMuPDF, used to recover true line geometry for Arabic content. Page
    numbers are 1-based to match Docling's ProvenanceItem.page_no."""
    pdf_doc = pymupdf.open(pdf_path)

    def lookup(page_no: int):
        if page_no < 1 or page_no > len(pdf_doc):
            return None, None
        page = pdf_doc[page_no - 1]
        return page, page.rect.height

    return lookup


def build_chunks(doc_id: str, doc, meta: dict, pdf_page_for=None) -> list[dict]:
    """meta comes from metadata_extraction.extract_metadata_fields, enriched
    with 'status' by status_rules.compute_statuses - not from a hand-authored
    index. 'title' is used for both languages: this corpus's PDFs only carry
    an English title, and showing that in Arabic answers too was a deliberate
    simplification over machine-translating a title at ingestion time.
    governs_note is gone entirely - governance relationships are now looked
    up from app.relationships_db at query time, not stored per-chunk."""
    body_chunks = chunk_document_body(doc, pdf_page_for)
    result = []
    seen_ids = {}
    for c in body_chunks:
        key = f"{doc_id}_{c['language']}_{c['chunk_type']}{c['clause_no']}"
        seen_ids[key] = seen_ids.get(key, 0) + 1
        chunk_id = key if seen_ids[key] == 1 else f"{key}_{seen_ids[key]}"
        result.append({
            "id": chunk_id,
            "text": c["text"],
            "metadata": {
                "doc_id": doc_id,
                "title": meta["title"],
                "category": meta["category"],
                "version": meta["version"],
                "effective_date": meta["effective_date"],
                "expiry_date": meta.get("expiry_date") or "",
                "approver_name": meta["approver_name"],
                "approver_role": meta["approver_role"],
                "status": meta["status"],
                "language": c["language"],
                "chunk_type": c["chunk_type"],
                "clause_no": c["clause_no"],
            },
        })
    return result


def main():
    pdf_paths = sorted(POLICY_DIR.glob("*.pdf"))
    print(f"Found {len(pdf_paths)} PDFs")

    # Force CPU for Docling's layout/OCR models. On Apple Silicon, MPS (GPU)
    # is a single shared memory pool with Ollama's embedding model, and the two
    # colliding causes GPU-OOM errors (kIOGPUCommandBufferCallbackErrorOutOfMemory).
    pipeline_options = PdfPipelineOptions()
    pipeline_options.accelerator_options = AcceleratorOptions(device=AcceleratorDevice.CPU)
    converter = DocumentConverter(
        format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=pipeline_options)}
    )

    # Phase 1: parse each PDF and extract its own metadata. Status isn't
    # known yet - it depends on comparing every document's dates/versions
    # together (supersession), which needs the whole set parsed first.
    parsed = []
    skipped = 0
    for pdf_path in pdf_paths:
        result = converter.convert(str(pdf_path))
        meta_fields = extract_metadata_fields(result.document)
        problems = validate_metadata(meta_fields, pdf_path.name)
        if problems:
            skipped += 1
            for p in problems:
                print(f"  SKIPPING: {p}")
            continue
        pdf_page_for = make_pdf_page_lookup(str(pdf_path))
        parsed.append({"meta": meta_fields, "doc": result.document, "pdf_page_for": pdf_page_for})

    if skipped:
        print(f"\n{skipped} document(s) skipped due to missing/invalid metadata.\n")

    # Phase 2: compute status across the whole set now that every
    # document's metadata is known.
    compute_statuses([p["meta"] for p in parsed])

    # Phase 3: chunk each document now that its status is settled. A
    # document superseded by a newer version of itself is deliberately
    # excluded here, not merely marked - its content is no longer the
    # answer to anything, the newer version's chunks are. This is
    # different from a standalone expired document with no replacement
    # (e.g. POL-IT-002): that one MUST stay searchable, since retrieval
    # finding it and assess_node seeing status="expired" is exactly what
    # triggers the "expired, contact the owner" refusal. Without this
    # split, two versions of the same doc_id would also collide on chunk
    # IDs (both compute the same "{doc_id}_{lang}_{type}{n}" key) -
    # confirmed directly: ingesting a real second version of POL-CASH-003
    # crashed with chromadb.errors.DuplicateIDError before this existed.
    all_chunks = []
    chunks_by_doc: dict[str, list[dict]] = {}
    for p in parsed:
        doc_id = p["meta"]["doc_id"]
        if p["meta"].get("superseded_by"):
            print(f"  {doc_id} (v{p['meta']['version']}): superseded by "
                  f"{p['meta']['superseded_by']}, excluded from the index")
            continue
        chunks = build_chunks(doc_id, p["doc"], p["meta"], p["pdf_page_for"])
        all_chunks.extend(chunks)
        chunks_by_doc[doc_id] = chunks
        print(f"  {doc_id} (v{p['meta']['version']}, {p['meta']['status']}): {len(chunks)} chunks")

    print(f"\nTotal chunks: {len(all_chunks)}")
    print("Embedding and loading into Chroma...")

    client = chromadb.PersistentClient(path=str(CHROMA_PATH))
    if COLLECTION_NAME in [c.name for c in client.list_collections()]:
        client.delete_collection(COLLECTION_NAME)
    collection = client.create_collection(COLLECTION_NAME)

    batch_size = 20
    for i in range(0, len(all_chunks), batch_size):
        batch = all_chunks[i:i + batch_size]
        texts = [c["text"] for c in batch]
        resp = ollama.embed(model=EMBED_MODEL, input=texts)
        embeddings = resp["embeddings"]
        collection.add(
            ids=[c["id"] for c in batch],
            embeddings=embeddings,
            documents=texts,
            metadatas=[c["metadata"] for c in batch],
        )
        print(f"  Loaded {min(i + batch_size, len(all_chunks))}/{len(all_chunks)} chunks")

    print(f"\nCollection '{COLLECTION_NAME}' has {collection.count()} chunks.")

    # Explicitly unload the embedding model before loading the generation
    # model for Stages 1-2: on a memory-constrained host (confirmed on both
    # this project's 8GB dev machine and, more tightly, inside Docker
    # Desktop's ~3.8GB VM) the two models' combined resident memory can
    # exceed what's available, and Ollama's default 5-minute keep-alive
    # means bge-m3 is often still loaded when gemma2:2b tries to load next -
    # observed directly as an OOM kill ("llama-server process has
    # terminated: signal: killed") when this wasn't done.
    ollama.generate(model=EMBED_MODEL, keep_alive=0)

    # Phase 4: Stage 1 - self-declared override relationships. One LLM call
    # per document, over its own English clause text.
    print("\nScanning for self-declared governance relationships (Stage 1)...")
    RELATIONSHIPS_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    declared_pairs = set()
    for doc_id, chunks in chunks_by_doc.items():
        en_clauses = [
            c["text"] for c in chunks
            if c["metadata"]["language"] == "en" and c["metadata"]["chunk_type"] == "clause"
        ]
        if not en_clauses:
            continue
        rel = detect_self_declared_relationship(doc_id, en_clauses)
        if rel:
            print(f"  {doc_id} declares it overrides {rel['target_doc']}")
            declared_pairs.add(tuple(sorted([doc_id, rel["target_doc"]])))

    # Phase 5: Stage 2 - similarity scan for undeclared conflicts between
    # current policies. Flags candidates for human review; never publishes
    # a relationship automatically from this stage.
    print("\nScanning for undeclared conflicts between current policies (Stage 2)...")
    flagged = scan_for_undeclared_conflicts(collection, declared_pairs)
    print(f"  {flagged} candidate conflict(s) flagged for human review "
          f"(see app.relationships_db.list_pending())")

    print(f"\nDone. {collection.count()} chunks at {CHROMA_PATH}")
    print(f"Relationships DB at {RELATIONSHIPS_DB_PATH}")


if __name__ == "__main__":
    main()
