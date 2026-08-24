FROM python:3.13-slim

# Docling's PDF/OCR stack (via onnxruntime/opencv) needs these at runtime.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1 \
    libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv

# Docling pulls in torch (and torchvision) transitively; pip's default
# resolution grabs the CUDA build, which drags in several GB of NVIDIA
# libraries this project never uses (Docling is explicitly pinned to CPU -
# see scripts/ingest.py). Installing both from the CPU-only index together,
# before requirements.txt can pull either in separately, cuts the image
# from ~10GB to ~2GB. Both together, in the same command, matters: pinning
# only torch here and letting torchvision resolve later from the default
# index installs mismatched build variants of the two - confirmed by
# actually running ingestion in a built container, which failed with
# "RuntimeError: operator torchvision::nms does not exist" (torchvision's
# custom ops registering against a torch ABI it wasn't built to match).
RUN pip install --no-cache-dir torch torchvision --index-url https://download.pytorch.org/whl/cpu

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ app/
COPY scripts/ scripts/
COPY policy_library/ policy_library/

# chroma_store and query_log.sqlite3 are runtime state, not baked into the
# image - mount them as a volume (see docker-compose.yml) so ingested data
# and the query log survive a container rebuild.
VOLUME ["/srv/chroma_store"]

EXPOSE 8000

CMD ["uvicorn", "app.server:app", "--host", "0.0.0.0", "--port", "8000"]
