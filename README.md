# Sovereign Policy Assistant

An on-premise assistant that answers staff questions about internal bank policy in
**English and Arabic**, grounded entirely in a local document library, and running
**fully offline** — no request ever leaves the machine. Every answer is backed by an
exact citation (document, version, effective date, approver) rendered from
structured metadata, not generated text, so it can never be fabricated. The
assistant also knows when *not* to answer plainly: it detects when two policies
disagree (if any such policy explccitly mentions that this one overrides another, then takes that into account, if not, then it sends the response for human review), refuses to treat an expired policy as current guidance, and honestly declines when a question isn't covered by any policy — logging that gap for a human to follow up on.

All of that metadata — document facts, policy status, governance relationships —
is derived automatically from the documents themselves. I made this decision choice so that the solution remains scalable with the addition of new policy docs.

This document explains what the project is built from, what every file does, and
how to run and export it. A companion file (use_cases.md), lists
example questions to test it with.

---

## 1. What it does — the four behaviors

Every question that reaches the assistant is routed into exactly one of four
behaviors, decided by code inspecting retrieved documents' metadata and a
governance-relationships database — **never** by asking the language model to
judge the situation itself:

**Normal answer**: A single current policy clearly covers the question => Answers from that policy's text, cites it
**Contradiction**: Two current policies disagree, and one explicitly states it overrides the other => Explains both, states plainly which one governs and why, cites both
**Expired refusal**: The only matching policy has expired and was never replaced => Refuses to state its contents as current guidance, points to the document owner |
**Out-of-scope refusal**: No policy is a good enough match => Says so honestly instead of guessing, logs the question for follow-up

This is the entire point of the project: a generic chatbot would've answered
confidently regardless of whether it should. This assistant is built so the *hard*
cases — contradiction, staleness, and "I don't know" — are handled correctly by
construction, not by hoping a language model gets it right.

---

## 2. Architecture
### 2.1. Ingestion Pipeline:
1. Docling & PyMuPDF parses the pdf based on its structure (heading, list items etc.)
2. status_rules.py computes each document's status from dates mentioned in the doc. If it has an expiry date which has passed, then the doc is considered expired, no answer will cite it.
3. chunking: This chunks the entire document based on docling's extracted structures
4. embedding: used bge-m3 because it supports multi-language embedding, previously tried bge-large & the results were horrible. it converts every chunk into a 1024-dim vector.
5. relationships: (i) detects governance relationships b/w docs (if any doc clearly mentions that this doc overrides another); (ii) for current documents' clauses, find other current documents' clauses that a similarity search says are on a similar topic, then ask an LLM whether they actually set conflicting requirements. Anything flagged goes to a human review queue.

### 2.2. Agent Pipeline:
1. This runs once per question
2. LangGraph nodes: **detect_language** => **retrieve from the chroma_store based on vector distance (Chroma's default, squared L2 - not cosine) over bge-m3 embeddings** => **assess** (decide: normal/contradiction/out-of-scope/expired) => **gather_context** (once a doc is identified, retrieves it's entire context, not just the matching chunk) => **generate** (write the answer, in the question's language) => **log_node**: writes every query to a log; for expired / out of scope queries, writes an escalation record.

## Unhappy paths:
For every question, it searches across the chroma_store for matches. After it finds the matching embeddings, 3 checks are done:

### Check 1 (Is the question covered at all?): 
The closest scoring match isn't accepted outright, the match score is higher than the threshold, then that means even though this particular doc is the best match, but it still doesn't answer the question.

### Check 2 (is a 2nd doc close enough) looking for contradictions:
The system checks whether there's another document which also scored close enough to the user query. If that's the case, then it checks the relationship table (sqlite) with the best matched doc's name; & finds out if during ingestion, we extracted info that this  doc overrides the 2nd best matched doc. If yes, then the generator will cite it as an contradiction & clearly say which this doc overrides this 2nd doc.

### check 3 (is the policy still valid):
if it's just one policy,then it checks if it's expired or still valid.

## 6. How to run it

**Prerequisites:** Docker Desktop (or another Docker Engine) running.

```bash
# 1. Bring up ollama + app + ui + watcher (pulls model weights into the
#    ollama container on first run - this step is slow only once)
docker compose up -d

# 2. Ingest the policy library - run once, inside the app container,
#    against the same models the running app will use. Needed only for
#    this first run; from here on, the watcher service (started as part
#    of step 1) re-runs this automatically whenever policy_library/ changes.
docker compose run --rm app python scripts/ingest.py
```

To get this running on a brand new machine from scratch (nothing installed yet):

```bash
# 0. Install Docker Desktop first, then clone the repo
git clone https://github.com/trayan4/Sovereign-Policy-Assistant.git
cd Sovereign-Policy-Assistant

# 1 & 2 same as above - docker compose up -d, then the ingest command

# 3. Open the UI
http://localhost:8501

# You'll hit a login screen - see the Security section below for the
# demo accounts. No account = no access, not even to ask a question.
```

One gotcha worth knowing: if you're pulling code changes into an already-running setup (not starting fresh) and `keycloak/realm-export.json` changed, a plain `docker compose up -d --build` won't pick that up - Compose only recreates a container when the image or its own config changes, not when the *contents* of a bind-mounted file change. Force it explicitly:

```bash
docker compose rm -sf keycloak
docker compose up -d keycloak
```

---

## 7. Security

Login is real, not decorative. Every request to `/ask` and every admin action needs a valid, signed token - there's no "standard" fallback for someone who isn't logged in at all.

Identity runs on **Keycloak**, self-hosted, one more service in the Docker stack (`docker-compose.yml`). It's the offline equivalent of Azure IAM/Entra ID - same underlying login standard (OIDC), same idea of users/roles/signed tokens, and it can be pointed at the bank's real Active Directory later instead of the three demo accounts below. Right now it runs in dev mode, which is fine for proving this works, not for production (no real database behind it, no TLS yet).

Three roles exist, and they're **not** a ladder - each one is a genuinely different kind of access, not a bigger or smaller version of the others:

**`standard_staff`**: can ask questions, gets answers from every policy except the 7 marked confidential (AML thresholds, PEP due diligence, risk-rating methodology, and a few fraud-sensitive approval-authority policies). If the only real answer to a question is in a confidential document, this role gets an honest "not covered" refusal, not a wrong answer from something else - I found and fixed a real bug during testing where filtering out the confidential doc made the system substitute a *different, incorrect* document instead of just saying it didn't know.

**`cleared_staff`**: everything standard_staff gets, plus confidential-document content. Citations for those show a visible 🔒 Confidential tag so it's obvious when you're looking at something restricted.

**`compliance_admin`**: doesn't grant confidential document access at all - it's a separate axis entirely. This is what unlocks the "Compliance dashboard" (query volume, pending governance-conflict review, and open service requests). A cleared_staff account can't see the dashboard; a compliance_admin account can't automatically read confidential policies. In a real bank these would usually be different people.

Which policies are confidential is decided by the document itself, not a list in the code - each one declares `Classification: Confidential` in its own metadata block, same convention as everything else (doc_id, version, approver). A document with no such line is standard by default.

### Demo credentials

| Username | Password | Role |
|---|---|---|
| `standard_user` | `demo1234` | `standard_staff` |
| `cleared_user` | `demo1234` | `cleared_staff` |
| `admin_user` | `demo1234` | `compliance_admin` |

These live in `keycloak/realm-export.json` and get created automatically the first time the Keycloak container starts. They're demo accounts, not a real identity source - a real rollout replaces them with the bank's actual directory, not more hand-typed users.