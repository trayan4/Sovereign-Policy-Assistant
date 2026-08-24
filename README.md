# Sovereign Policy Assistant

An on-premise assistant that answers staff questions about internal bank policy in
**English and Arabic**, grounded entirely in a local document library, and running
**fully offline** — no request ever leaves the machine. Every answer is backed by an
exact citation (document, version, effective date, approver) rendered from
structured metadata, not generated text, so it can never be fabricated. The
assistant also knows when *not* to answer plainly: it detects when two policies
disagree and explains which one governs, refuses to treat an expired policy as
current guidance, and honestly declines when a question isn't covered by any
policy — logging that gap for a human to follow up on.

All of that metadata — document facts, policy status, governance relationships —
is **derived automatically from the documents themselves**, not hand-typed into a
config file. That's a deliberate design point, not an implementation detail: a
system that only works because someone manually curated 40 example documents
doesn't scale to a real policy library with hundreds of PDFs arriving over time.

This document explains what the project is built from, what every file does, and
how to run and export it. A companion file, [`use_cases.md`](use_cases.md), lists
example questions to test it with.

---

## 1. What it does — the four behaviors

Every question that reaches the assistant is routed into exactly one of four
behaviors, decided by code inspecting retrieved documents' metadata and a
governance-relationships database — **never** by asking the language model to
judge the situation itself:

| Behavior | Trigger | What happens |
|---|---|---|
| **Normal answer** | A single current policy clearly covers the question | Answers from that policy's text, cites it |
| **Contradiction** | Two current policies disagree, and one explicitly states it overrides the other | Explains both, states plainly which one governs and why, cites both |
| **Expired refusal** | The only matching policy has expired and was never replaced | Refuses to state its contents as current guidance, points to the document owner |
| **Out-of-scope refusal** | No policy is a good enough match | Says so honestly instead of guessing, logs the question for follow-up |

This is the entire point of the project: a generic chatbot answers confidently
regardless of whether it should. This assistant is built so the *hard* cases —
contradiction, staleness, and "I don't know" — are handled correctly by
construction, not by hoping a language model gets it right.

---

## 2. Architecture

```
┌───────────────────────────────────────────────────────────────────────────┐
│  INGESTION (batch job: python scripts/ingest.py)                          │
│                                                                             │
│  policy_library/*.pdf                                                      │
│         │                                                                  │
│         ▼                                                                  │
│    Docling (parses PDF structure: headings, list items, text)             │
│         │                                                                  │
│         ▼                                                                  │
│    PyMuPDF (word-level geometry, fixes Arabic RTL text-extraction         │
│              ordering that Docling's flattened text loses)                │
│         │                                                                  │
│         ▼                                                                  │
│    Metadata extraction (doc_id, version, dates, approver - read           │
│    directly from each PDF's own metadata block, not a config file)        │
│         │                                                                  │
│         ▼                                                                  │
│    Status computation (current/expired - computed from dates and         │
│    version supersession, recalculated every run, never stored)            │
│         │                                                                  │
│         ▼                                                                  │
│    Chunking (clause-level) + Ollama embedding (bge-m3, multilingual)      │
│         │                                                                  │
│         ▼                                                                  │
│    ChromaDB (local persistent vector store)  →  chroma_store/             │
│         │                                                                  │
│         ▼                                                                  │
│    Relationship detection (two stages, see §5):                           │
│      Stage 1 - self-declared overrides (document says so itself,          │
│                auto-published)                                            │
│      Stage 2 - similarity scan for undeclared conflicts (flagged for      │
│                human review, never auto-published)                        │
│         │                                                                  │
│         ▼                                                                  │
│    relationships.sqlite3 (live relationships + pending review queue)      │
└───────────────────────────────────────────────────────────────────────────┘

┌───────────────────────────────────────────────────────────────────────────┐
│  QUERY TIME (the running service)                                         │
│                                                                             │
│  Streamlit UI  →  FastAPI (/ask)                                          │
│                        │                                                   │
│                        ▼                                                   │
│  LangGraph pipeline (app/graph.py):                                       │
│                                                                             │
│   detect_language → retrieve → assess → gather_context                   │
│                                    │                                        │
│                        (decides: normal / contradiction / expired /       │
│                         out_of_scope, from Chroma metadata + the          │
│                         relationships database - never the LLM)           │
│                                    │                                        │
│                                    ▼                                       │
│                               generate (Ollama chat model, gemma2:2b)     │
│                                    │                                        │
│                                    ▼                                       │
│                                  log (SQLite query log + escalations)     │
│                                    │                                        │
│                                    ▼                                       │
│                    answer + citations (built from metadata,               │
│                    never trusted to the LLM's own text)                   │
└───────────────────────────────────────────────────────────────────────────┘
```

Three separate concerns, three separate entry points:
- **Ingestion** (`scripts/ingest.py`) is a batch job. Run it once, or again
  whenever the policy PDFs change.
- **The backend** (`app/`) is a long-running FastAPI service that answers
  questions against whatever is currently in the vector store, plus a small
  admin API for the human-review queue.
- **The UI** (`ui/`) is a thin Streamlit front end that talks to the backend
  over HTTP - it never touches Chroma, Ollama, or the LangGraph pipeline
  directly.

---

## 3. Technology stack

| Technology | Role | Why this one |
|---|---|---|
| **[Docling](https://github.com/docling-project/docling)** | PDF parsing into structured document objects (headings, list items, paragraphs) | Open-source, layout-aware — distinguishes intro prose from enumerated clauses on its own, rather than needing us to guess document structure from raw text |
| **[PyMuPDF](https://pymupdf.readthedocs.io/)** (`fitz`) | Word-level PDF geometry (bounding boxes, per-line grouping) | Fixes a real bug: Docling's flattened text loses line boundaries needed to correctly reorder Arabic (RTL) text extracted from certain PDFs |
| **[Ollama](https://ollama.com/)** | Local model runtime for both embeddings and generation | Keeps every model call on-machine — the whole point of "sovereign" — with a simple HTTP API and a large library of open models |
| **`bge-m3`** (via Ollama) | Embedding model (1.2GB) | Genuinely multilingual (100+ languages including Arabic) — an earlier choice, `bge-large`, was English-only and silently returned wrong results for Arabic queries; verified via direct similarity testing before switching |
| **`gemma2:2b`** (via Ollama) | Generation model, and the extraction model for relationship detection (1.6GB) | Small enough to run reliably on constrained hardware (this project's dev machine has 8GB RAM total); larger models (7–8B) were tested and repeatedly hung the machine |
| **[ChromaDB](https://www.trychroma.com/)** | Local vector database | Persists to a plain directory (`chroma_store/`), no server process to run, trivial to back up or ship |
| **[LangGraph](https://langchain-ai.github.io/langgraph/)** | Orchestration of the retrieve → decide → generate → log pipeline | Makes the four behaviors explicit graph nodes/edges instead of one large prompt trying to do everything — each step is independently testable and the control flow (which behavior applies) is ordinary Python, not something the LLM has to get right |
| **[FastAPI](https://fastapi.tiangolo.com/)** + **Uvicorn** | HTTP API serving the graph | Auto-generates interactive API docs (`/docs`) for free, minimal boilerplate |
| **[Streamlit](https://streamlit.io/)** | Front end | A working UI in a single file, no separate build/JS toolchain — appropriate for a focused internal tool talking to an already-built API |
| **SQLite** (Python stdlib `sqlite3`) | Query log, escalation queue, and the relationships/pending-review database | Zero setup, plain files, sufficient for this scale |
| **Docker + Docker Compose** | Packaging and deployment | Three services — `ollama`, `app`, `ui` — so the whole stack (including model weights, via a one-shot pull step) comes up with one command on any machine |

---

## 4. Directory structure

```
sovereign_policy_assistant/
├── policy_library/          40 synthetic bilingual policy PDFs
│   └── POL-*.pdf              (the only source of truth - no separate index file)
│
├── scripts/                 The ingestion pipeline
│   ├── ingest.py              Orchestrates the whole pipeline (see §5)
│   ├── metadata_extraction.py Reads each PDF's own metadata block
│   ├── status_rules.py        Computes current/expired from dates
│   └── relationships.py       Derives governance relationships from content
│
├── app/                     The running backend service
│   ├── config.py               All tunable constants in one place
│   ├── retrieval.py             Embedding + Chroma query helpers
│   ├── prompts.py                 System prompts for each of the 4 behaviors
│   ├── graph.py                     The LangGraph pipeline itself
│   ├── query_log.py                   SQLite query log + escalation queue
│   ├── relationships_db.py              Live relationships + human review queue
│   ├── main.py                            CLI entrypoint
│   └── server.py                            FastAPI HTTP entrypoint
│
├── ui/                       The front end (own image, own dependencies)
│   ├── streamlit_app.py         The whole UI
│   ├── requirements.txt          streamlit + requests only
│   └── Dockerfile
│
├── chroma_store/             Runtime data (generated by ingestion, not
│   ├── chroma.sqlite3          committed to source control in a real project):
│   ├── <uuid>/                 the vector store,
│   ├── query_log.sqlite3       the query log + escalations, and
│   └── relationships.sqlite3   governance relationships + pending review queue -
│                                all three kept in one persisted directory
│
├── requirements.txt          Pinned backend dependencies (venv + Docker)
├── Dockerfile                 Builds the `app` image
├── docker-compose.yml          Orchestrates `ollama` + `app` + `ui` together
├── .dockerignore
│
├── README.md                 This file
└── use_cases.md               Example questions to test the assistant with
```

---

## 5. File-by-file explanation

### `policy_library/*.pdf` (40 files)

Synthetic policy documents standing in for a real bank's policy library, covering
9 categories (Cash Handling & Deposits, Leave & Time Off, Approval & Waiver
Authority, Expense & Reimbursement, Data Handling & Information Security, HR
Conduct & Disciplinary, Customer Onboarding & KYC, IT & Systems Access,
Procurement). Each document has a **Purpose** statement and 3 numbered
**Clauses**, written in both English and Arabic within the same PDF, plus a
metadata block (Document ID, Category, Version, Effective Date, Approver) that
ingestion reads directly - there's no separate index file anywhere in this
project.

Two documents are deliberately special, to exercise the assistant's unhappy-path
behaviors:
- **`POL-CASH-001` + `POL-CASH-002`** — a genuine contradiction. `POL-CASH-001`
  (Group Policy) sets a AED 50,000 daily cash deposit limit; `POL-CASH-002`
  (Dubai Branch Addendum) sets AED 35,000 for the Dubai Main Branch specifically,
  and its own clause text explicitly says it overrides the group policy for that
  branch - this is discovered by ingestion, not declared in a config file.
- **`POL-IT-002`** (Remote Work Equipment Reimbursement Policy) — effective
  2023-06-01, with an expiry date of 2024-06-01 printed in its own metadata
  block, never replaced. Its `expired` status is *computed* from those two
  dates against today's date, every time ingestion runs.

### `scripts/ingest.py`

The full ingestion pipeline, run as `python scripts/ingest.py`. Five phases:

1. **Parse each PDF with Docling**, forcing CPU execution
   (`AcceleratorDevice.CPU`) — deliberately, because Docling's layout model
   defaults to Apple Silicon's GPU (Metal/MPS), which shares memory with
   whatever Ollama has loaded; running both at once caused GPU-OOM crashes
   during development. Chunking (`chunk_document_body`) walks Docling's
   structured items - headings, metadata blocks, and body content are told
   apart by Docling's own item *type*, never by matching a literal string
   like `"[EN] English"`. **Arabic word order** gets fixed here too: PDF text
   extraction (confirmed on both Docling's and raw PyMuPDF's output) returns
   Arabic in *visual* order rather than *logical* reading order. A single-
   line clause needs a straightforward reversal; a clause embedding a Latin
   reference (`POL-CASH-001`, `BYOD`) needs the reversal to skip over that
   reference rather than reverse through it; and a clause that **wraps across
   two PDF lines** needs `get_pymupdf_lines_for_item` to recover the true
   line geometry via PyMuPDF's word bounding boxes, since Docling's flattened
   text loses the line boundary entirely.
2. **Extract metadata** (`metadata_extraction.py`) from each PDF's own
   metadata block - `doc_id`, `category`, `version`, `effective_date`,
   `expiry_date`, `approver_name`/`approver_role`, `title` - via generic
   label:value parsing, not hardcoded per-document strings. A document
   missing a required field is skipped with a loud warning, not silently
   ingested with gaps.
3. **Compute status** (`status_rules.py`) across the whole set at once (this
   needs every document together, because supersession - an older version of
   a re-issued policy - requires comparing documents' `doc_id`s and versions
   against each other). `status` is never stored; it's recalculated from
   `effective_date`/`expiry_date` every single run.
4. **Embed and load into Chroma** via `bge-m3`, in batches of 20. The
   embedding model is then explicitly unloaded (`keep_alive=0`) before the
   next phase - see the note on Docker memory below.
5. **Detect governance relationships** (`relationships.py`), two stages:
   - **Stage 1 (self-declared):** one LLM call per document, asking whether
     its own text explicitly states it overrides another specific policy.
     Auto-publishes to the live `relationships` table - the document is
     self-reporting, so this is treated as trusted. A regex guardrail
     (`OVERRIDE_LANGUAGE_RE`) requires the captured evidence to actually
     contain override language (*supersedes/overrides/takes precedence/
     governs*) before trusting the model's judgment - added after testing
     found `gemma2:2b` calling a plain cross-reference ("disciplinary
     offence under POL-HR-003") a declared override.
   - **Stage 2 (undeclared conflicts):** for current documents' clauses,
     finds other current documents' clauses a similarity search flags as
     topically similar, then asks the LLM whether they actually conflict.
     Anything flagged goes to `pending_relationships` for human review and
     is **never** auto-published - confirmed necessary in testing, where 3
     of 4 flagged candidates had model `reasoning` text that explicitly
     said "different topics" while the `conflicting` flag said `true`
     anyway. The human-review gate is doing real, load-bearing work here,
     not just defensive padding.

### `scripts/metadata_extraction.py`

Generic `Label: Value` parsing of whatever block Docling identifies as
metadata (detected by the word "metadata" appearing in a heading - a common
convention, not a fixed schema), with a label-alias map so wording variation
("Doc ID" vs "Document ID") doesn't break extraction. Deliberately does
**not** extract a document's own "Status" or "Governance Note" fields even
where a source PDF happens to carry them - this project computes status from
dates and derives relationships from content instead of trusting a static
label, since a real ingestion pipeline can't assume every source PDF
self-reports those correctly (and testing proved that assumption wrong for
relationships specifically - see above).

### `scripts/status_rules.py`

```
status = "current" if effective_date <= today
                   AND (no expiry_date OR expiry_date >= today)
                   AND not superseded by a newer version of the same doc_id
          else "expired"
```

Grouping by `doc_id` and comparing versions means a policy re-issued as a new
PDF under the same Document ID automatically supersedes the older one - no
one has to flip a flag. In the current 40-document corpus every `doc_id` is
unique (one PDF each), so this mechanism is a no-op today, but it's what
makes a future re-issued policy handle itself correctly without a code
change.

### `scripts/relationships.py`

Implements Stage 1 and Stage 2 described above. Uses `ollama.chat(...,
format="json")` for structured output, with defensive parsing (a malformed
response degrades to "no relationship found" rather than crashing
ingestion). Stage 2 reuses each clause's embedding already stored in Chroma
(`include=["embeddings"]`) rather than re-embedding via Ollama, since the
vectors already exist.

### `app/config.py`

Every tunable constant, in one place, each with a comment explaining *why*
its value is what it is:

- `EMBED_MODEL` / `GEN_MODEL` — which Ollama models to use.
- `OUT_OF_SCOPE_DISTANCE` (0.85) — the Chroma cosine-distance cutoff above
  which a question is treated as not covered by any policy. Calibrated by
  testing real on-topic questions (scored 0.6–0.8) against nonsense/off-topic
  ones ("What's the weather today?", scored 1.1+).
- `CONTRADICTION_MARGIN` (0.22) — how much worse than the single best match a
  second document's own best chunk is allowed to be and still count as a
  genuine rival answer (as opposed to a merely topically-adjacent document).
  Calibrated against this corpus: the real `POL-CASH-001`/`POL-CASH-002` pair
  sits ~0.19 apart; an unrelated document in the same general topic area sits
  ~0.29 apart.
- `RETRIEVE_N` (8) — how many chunks to pull per query before filtering.

### `app/retrieval.py`

- `detect_language(text)` — classifies by actual Unicode content, not by an
  assumed input format.
- `retrieve(question, language, n)` — embeds the question via `bge-m3` and
  queries Chroma, filtered to chunks in the question's own language.
- `get_document_chunks(doc_id, language)` — once a target document is
  identified, pulls its *entire* chunk set (purpose + all clauses), so the
  model sees the whole policy rather than one sentence stripped of context.

### `app/prompts.py`

One system prompt per behavior (`SYSTEM_NORMAL`, `SYSTEM_CONTRADICTION`,
`SYSTEM_EXPIRED`, `SYSTEM_OUT_OF_SCOPE`), plus `build_user_prompt`, which
assembles the retrieved context and the question — and repeats the target
language directive at **both** the start and end of the user turn, and again
in the system message itself (`_generate` in `graph.py`).

That repetition is a deliberate fix, not decoration: `gemma2:2b` (a
2B-parameter model) was observed to answer in English despite an Arabic
question and an explicit system-level instruction, especially on longer
prompts (the contradiction case, with two documents' worth of context).

`FOLLOW_UP_SENTENCE` is a fixed, code-templated sentence ("Please see the
source details below for who to contact") appended to expired-case answers —
**not** generated by the LLM. Early testing showed the model would fabricate
a placeholder (`"[Document Owner Name]"`) when asked to reference a name it
hadn't actually been given; the fix was to stop asking it to write that part
at all.

### `app/graph.py`

The LangGraph pipeline. Six nodes, one straight-line path:

```
detect_language → retrieve → assess → gather_context → generate → log
```

- **`assess_node`** is the core decision logic, and it never asks the LLM
  anything. It groups retrieved chunks by document, filters to genuine
  candidates (see `CONTRADICTION_MARGIN`), checks each candidate's `status`
  in Chroma metadata, and looks up `app.relationships_db.get_relationship_for_doc`
  for a declared override — both computed/derived facts, not hand-authored.
- **`generate_node`** builds a case-specific prompt and calls `gemma2:2b`.
  Citations are built directly from Chroma metadata and the relationships
  database, not extracted from the model's own text — the model is never
  trusted to correctly restate a clause number, version, date, or approver
  name. It only ever writes the natural-language explanation; the facts are
  rendered separately, from data.
- **`log_node`** records every query and, for `expired`/`out_of_scope`
  cases, an escalation row — the mock "service desk ticket queue" from the
  project plan.

### `app/relationships_db.py`

Two SQLite tables: `relationships` (live, trusted - read by `assess_node`)
and `pending_relationships` (Stage 2 candidates awaiting a human decision,
never read by `assess_node`). `review_pending(id, approve, note)` promotes an
approved candidate into the live table; rejecting just records the decision.
Both leave an audit trail (`origin`, timestamps, reviewer note).

### `app/query_log.py`

Two SQLite tables: `queries` (every question asked) and `escalations` (rows
for questions the assistant couldn't confidently answer, with a reason and,
where known, an owner to follow up with). `department_summary()` aggregates
query volume and open-escalation count per department.

### `app/main.py`

CLI entrypoint:

```bash
python -m app.main "What is the daily cash withdrawal limit without prior notice?"
python -m app.main "سؤال بالعربية" --department "Retail Banking"
python -m app.main "..." --json   # raw JSON output instead of formatted text
```

### `app/server.py`

FastAPI app exposing:

- `GET /health` — liveness check.
- `POST /ask` — `{"question": "...", "department": "..."}` → answer + case +
  citations.
- `GET /departments` — the department summary.
- `GET /admin/pending-relationships` — the Stage 2 review queue.
- `POST /admin/pending-relationships/{id}/review` — `{"approve": true/false,
  "note": "..."}` to approve or reject a candidate.

Visiting `http://localhost:8000/docs` gives an interactive UI for all of
these, generated automatically by FastAPI.

### `ui/streamlit_app.py`

The front end: a question box (English or Arabic) with an optional
department field, an answer panel with a clear behavior label (✅ answered /
⚠️ conflict / ⏱️ expired / ❔ not covered) and, alongside it, source citation
cards (title, version, status, effective date, approver, governance note
highlighted when relevant). A collapsed "Compliance dashboard" section holds
two tabs: query volume by department, and the pending-conflicts review queue
with working Approve/Reject buttons that call the admin API directly.

Talks to the backend over plain HTTP (`API_URL` env var, defaults to
`http://localhost:8000`) - it has no dependency on Chroma, Ollama, or the
LangGraph pipeline, which is why it ships as its own much smaller Docker
image (`ui/Dockerfile`, ~790MB vs. the backend's ~3GB).

### `requirements.txt` / `ui/requirements.txt`

Pinned exact versions of every runtime dependency, kept deliberately
separate: the backend's list (`docling`, `pymupdf`, `chromadb`, `ollama`,
`langgraph`, `fastapi`, `uvicorn`, `pydantic`) is heavy; the UI's
(`streamlit`, `requests`) is not, and there's no reason for the two to share
an image or a dependency list.

### `Dockerfile`

Builds the `app` image. Two things worth knowing:

- **Installs `torch` and `torchvision` together, from the CPU-only wheel
  index, in one command.** Docling depends on both transitively; pinning
  only `torch` to the CPU build while letting `torchvision` resolve
  separately from the default index installs *mismatched* build variants of
  the two — confirmed directly by running ingestion inside a built
  container, which crashed with `RuntimeError: operator torchvision::nms
  does not exist` until both were pinned together. This single fix also cut
  the image from **9.84GB to ~3.1GB** (avoiding several GB of unused NVIDIA
  libraries pulled in by the default CUDA build).
- `chroma_store/` is declared a `VOLUME` — runtime state, not baked into the
  image, so ingested data, the query log, and the relationships database all
  survive a rebuild (see `docker-compose.yml`'s bind mount).

### `docker-compose.yml`

Four services:

1. **`ollama`** — the official `ollama/ollama` image, model weights persisted
   in a named volume (`ollama_data`).
2. **`ollama-init`** — a one-shot container that pulls `gemma2:2b` and
   `bge-m3`, then exits. Retries a few times on failure — `ollama pull` run
   this way was observed to fail intermittently on the very first attempt
   right after the server reports healthy, succeeding reliably on retry (a
   startup race, not a real incompatibility).
3. **`app`** — builds from the `Dockerfile`, waits for `ollama-init` to
   complete and exposes a healthcheck of its own (`/health`) so `ui` can wait
   for *it* in turn. Binds `chroma_store/` from the host.
4. **`ui`** — builds from `ui/Dockerfile`, waits for `app` to be healthy,
   talks to it via `API_URL=http://app:8000` over Docker's internal network.

**A memory note that matters beyond Docker too:** running ingestion inside
Docker Desktop's VM (confirmed at only ~3.8GB total via `docker stats`)
surfaced an OOM kill of the generation model, because the embedding model was
still resident (Ollama's default 5-minute keep-alive) when the generation
model tried to load on top of it. `scripts/ingest.py` now explicitly unloads
the embedding model (`keep_alive=0`) before the relationship-detection phase
starts — a real robustness fix for any memory-constrained host, not a
Docker-only workaround.

### `.dockerignore`

Keeps `.venv/`, `__pycache__/`, `chroma_store/`, and the query log out of the
Docker build context.

---

## 6. How to run it

### Option A — Local (Python virtualenv + Ollama on the host)

**Prerequisites:** Python 3.13, [Ollama](https://ollama.com/) installed and
running.

```bash
# 1. Pull the two models this project uses
ollama pull gemma2:2b
ollama pull bge-m3

# 2. Set up the virtualenv
python3.13 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 3. Ingest the policy library (builds chroma_store/)
python scripts/ingest.py

# 4a. Ask a question via the CLI
python -m app.main "What is the maximum daily cash deposit limit at the Dubai Main Branch?"

# 4b. ...or run the API server
uvicorn app.server:app --reload
# then open http://localhost:8000/docs

# 4c. ...or run the Streamlit UI (in a separate terminal, with the API server running)
pip install -r ui/requirements.txt
streamlit run ui/streamlit_app.py
# then open http://localhost:8501
```

### Option B — Docker (the portable deployment)

**Prerequisites:** Docker Desktop (or another Docker Engine) running.

```bash
# 1. Bring up ollama + app + ui (pulls model weights into the ollama
#    container on first run - this step is slow only once)
docker compose up -d

# 2. Ingest the policy library - run once, inside the app container,
#    against the same models the running app will use
docker compose run --rm app python scripts/ingest.py

# 3. Open the UI
#    http://localhost:8501

# ...or test the API directly:
curl -X POST http://localhost:8000/ask \
  -H "Content-Type: application/json" \
  -d '{"question": "What is the daily cash withdrawal limit without prior notice?"}'

# or the interactive API docs: http://localhost:8000/docs
```

**Useful commands:**

```bash
docker compose ps                 # check service status
docker compose logs -f app        # stream the backend's logs live
docker compose logs -f ui         # stream the UI's logs live
docker compose down               # stop everything (keeps model weights + ingested data)
docker compose down -v            # stop everything AND delete model weights (re-pulls next time)
```

Ingested data (the vector store, query log, and relationships database) lives
in `./chroma_store/` on the host, bind-mounted into the `app` container, so it
survives `docker compose down` and image rebuilds. **Caveat observed in
testing:** if you delete and recreate that host directory while `app` is
already running (rather than stopping it first), its bind mount can go stale
on macOS/Docker Desktop — restart the service (`docker compose restart app`)
if `/ask` or the admin endpoints start returning file-not-found errors after
doing that.

---

## 7. How to export it

The whole point of the Docker packaging is that "exporting" this project means
handing someone three things: the `Dockerfile`s (backend and UI), `docker-
compose.yml`, and the application code (which is what building the images
bakes in). There's no separate "export" step beyond that — but a few concrete
ways to actually hand it off:

### Export as portable image files (no registry needed)

```bash
# Build both (if not already built)
docker compose build

# Save each to a file
docker save sovereign_policy_assistant-app -o spa-app.tar
docker save sovereign_policy_assistant-ui -o spa-ui.tar

# On the receiving machine
docker load -i spa-app.tar
docker load -i spa-ui.tar
```

The recipient still needs `docker-compose.yml` (to also bring up the `ollama`
service) and, if they want to change the ingested documents, `policy_library/`
and `scripts/`. The simplest full export is the whole project directory plus
the two saved images.

### Export via a container registry (for a team / repeated deployment)

```bash
docker tag sovereign_policy_assistant-app your-registry/spa-app:latest
docker tag sovereign_policy_assistant-ui your-registry/spa-ui:latest
docker push your-registry/spa-app:latest
docker push your-registry/spa-ui:latest
```

Then anyone with access to that registry can `docker compose up` against a
`docker-compose.yml` pointing each service's `image:` at those tags instead
of `build:`.

### Export the whole project (source form)

Since there's no compiled build step beyond the Docker images themselves, the
project directory *is* the deliverable — copy or `git clone` it, and
`docker compose up` (+ the one-time ingestion command) reproduces the exact
same running system anywhere Docker is available. This is the form to use if
the recipient needs to modify the policy library or the assistant's logic,
not just run it as-is.

---

## 8. Known limitations

- **Retrieval thresholds are calibrated against this specific 40-document
  corpus** (`OUT_OF_SCOPE_DISTANCE`, `CONTRADICTION_MARGIN` in `config.py`).
  A larger or differently-distributed real policy library would need these
  re-tuned.
- **`gemma2:2b` is a small model**, used for both generation and relationship
  extraction. It occasionally needs its language instruction reinforced
  (already handled) and produces slightly less polished prose on the more
  complex contradiction case. More importantly, testing found it genuinely
  unreliable at judging document relationships on its own: on a fresh run
  across all 40 documents, 7 got a raw "this overrides something" signal,
  only 1 was real - the other 6 were either hallucinated targets or, in one
  case, a plain cross-reference misjudged as a governance claim. The
  code-level guardrails (regex keyword check for Stage 1, mandatory human
  review for Stage 2) exist specifically because the model's own judgment
  isn't trustworthy enough to publish from directly. A production deployment
  with a larger extraction model might need fewer of these guardrails, but
  should keep the human-review step for Stage 2 regardless - a wrong "which
  policy governs" answer has real compliance consequences.
- **A handful of Arabic clauses have a minor, known font-rendering artifact**
  from the source PDFs — certain words with specific diacritic combinations
  get an extra internal space. This is a source-PDF character-spacing issue,
  not a word-order or retrieval bug, and doesn't affect which clause is
  retrieved or its overall meaning.
- **Memory-constrained environments need the explicit model-unload step**
  ingestion already does (see `Dockerfile`/`docker-compose.yml` notes above).
  Running generation and embedding models concurrently on an 8GB host, or
  inside a ~4GB Docker Desktop VM, will OOM without it.
