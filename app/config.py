from pathlib import Path

ROOT = Path(__file__).parent.parent
CHROMA_PATH = ROOT / "chroma_store"
COLLECTION_NAME = "policy_library"
# Lives inside the same persisted directory as the vector store, rather
# than a separate top-level file, so Docker only needs one volume mount to
# keep both the embeddings and the query log across container rebuilds.
QUERY_LOG_PATH = CHROMA_PATH / "query_log.sqlite3"

EMBED_MODEL = "bge-m3"
GEN_MODEL = "gemma2:2b"

# Not read here: the ollama client picks up the OLLAMA_HOST environment
# variable itself (defaults to http://localhost:11434 if unset). Set it via
# docker-compose's environment section to point at the ollama service.

# Chroma cosine distance (0 = identical, 2 = opposite). Calibrated against
# this corpus: genuinely on-topic questions score ~0.6-0.8, nonsense/off-
# topic questions score 1.1+, with a gray zone in between. A question whose
# best match falls above this is treated as not covered by the policy set.
OUT_OF_SCOPE_DISTANCE = 0.85

# How much worse than the single best match a document's own best chunk is
# allowed to be and still count as a genuine rival answer (used to decide
# whether a second document is actually competing to answer the SAME
# question, e.g. two deposit-limit policies, vs. just a distantly related
# document that happens to surface within the general relevance threshold,
# e.g. a deposit policy surfacing for a withdrawal-limit question).
# Calibrated against this corpus: a real contradicting pair sits ~0.19
# apart; an unrelated same-topic-area document sits ~0.29 apart.
CONTRADICTION_MARGIN = 0.22

# How many chunks to retrieve per query before filtering down to what's
# actually relevant (RETRIEVE_N) vs. how many distinct source documents to
# consider when looking for a contradiction between two current policies.
RETRIEVE_N = 8
