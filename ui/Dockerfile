FROM python:3.13-slim

# Deliberately its own lightweight image, separate from the backend's
# Dockerfile: this UI only ever talks to the backend over HTTP, so it has
# no reason to carry Docling/PyTorch's multi-GB footprint.
WORKDIR /srv

COPY ui/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY ui/streamlit_app.py .
COPY ui/.streamlit .streamlit

EXPOSE 8501

CMD ["streamlit", "run", "streamlit_app.py", "--server.address=0.0.0.0", "--server.port=8501", "--server.headless=true"]
