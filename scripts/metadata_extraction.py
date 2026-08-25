"""Extracts structured document metadata (doc_id, category, version,
effective_date, expiry_date, approver) directly from a PDF's own metadata
block, via Docling's structured items - not from any separately maintained
index file. A document is the source of truth for its own facts.

Deliberately does NOT extract 'Status' or 'Governance Note' even where a
source PDF happens to carry them: this project computes status from dates
(see status_rules.py) and derives governance/override relationships from
document content (see relationships.py) rather than trusting a static label,
since a real ingestion pipeline can't assume every source PDF will (or
should) self-report those correctly."""

import re

NON_CONTENT_LABELS = {
    "section_header", "title", "page_header", "page_footer", "table",
    "picture", "chart", "formula", "document_index", "checkbox_selected",
    "checkbox_unselected", "form", "key_value_region", "field_region",
    "field_heading", "field_item", "field_key", "field_value", "field_hint",
    "grading_scale", "empty_value", "marker", "caption",
}

# Label text -> canonical field name. Matched case-insensitively against
# whatever precedes the first colon on a metadata line, so it survives label
# wording variation ("Doc ID" vs "Document ID") without needing an exact
# hardcoded string per corpus.
LABEL_ALIASES = {
    "document id": "doc_id",
    "doc id": "doc_id",
    "id": "doc_id",
    "category": "category",
    "version": "version",
    "effective date": "effective_date",
    "expiry date": "expiry_date",
    "expiration date": "expiry_date",
    "approver": "approver_raw",
    "classification": "classification",
}


def extract_title(doc) -> str:
    """The document's own first-level heading - used as-is for citations in
    both languages (this corpus's PDFs only carry an English title; falling
    back to it for Arabic answers is a deliberate, simpler choice over
    machine-translating a title at ingestion time)."""
    for item, _level in doc.iterate_items():
        if getattr(item, "label", "") == "section_header":
            text = getattr(item, "text", "").strip()
            if text:
                return text
    return ""


def extract_metadata_fields(doc) -> dict:
    """Walks the document's structured items, extracting label:value pairs
    from whichever heading-delimited block is generically identifiable as
    metadata (a heading containing the word 'metadata' - a common document
    convention, not a fixed schema)."""
    fields: dict[str, str] = {}
    in_metadata = False

    for item, _level in doc.iterate_items():
        label = getattr(item, "label", "")
        text = getattr(item, "text", "").strip()

        if label == "section_header":
            in_metadata = "metadata" in text.lower()
            continue
        if label in NON_CONTENT_LABELS or not text or not in_metadata:
            continue
        if ":" not in text:
            continue

        key_raw, _, value = text.partition(":")
        canonical = LABEL_ALIASES.get(key_raw.strip().lower())
        if canonical and value.strip():
            fields[canonical] = value.strip()

    if "approver_raw" in fields:
        name, _, role = fields.pop("approver_raw").partition(",")
        fields["approver_name"] = name.strip()
        fields["approver_role"] = role.strip()

    # Optional field - most documents don't declare one at all, which means
    # "nothing restricted about this document", not "unknown". Normalized
    # to lowercase so "Confidential" and "confidential" are treated the same.
    fields["classification"] = fields.get("classification", "standard").strip().lower()

    fields["title"] = extract_title(doc)
    return fields


DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def validate_metadata(fields: dict, source_name: str) -> list[str]:
    """Returns a list of problems (empty if none) - required fields missing
    or malformed. Ingestion should surface these loudly rather than silently
    proceeding with an incomplete record, since a document a compliance
    officer can't be cited correctly for is worse than one that failed to
    ingest at all."""
    problems = []
    required = ["doc_id", "category", "version", "effective_date", "approver_name"]
    for field in required:
        if not fields.get(field):
            problems.append(f"{source_name}: missing required field '{field}'")
    for date_field in ("effective_date", "expiry_date"):
        value = fields.get(date_field)
        if value and not DATE_RE.match(value):
            problems.append(f"{source_name}: '{date_field}' = {value!r} is not YYYY-MM-DD")
    return problems
