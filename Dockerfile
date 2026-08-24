FROM python:3.11-slim

# System deps: gcc for any wheels that need building, curl for the healthcheck
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# --- Install Python dependencies first (better layer caching) -------------
COPY requirements.txt ./requirements.txt
COPY agent/requirements.txt ./agent-requirements.txt
COPY mcp_server/requirements.txt ./mcp_server-requirements.txt
COPY rag/requirements.txt ./rag-requirements.txt
COPY ib_platform/backend/requirements.txt ./backend-requirements.txt

RUN pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir -r agent-requirements.txt \
    && pip install --no-cache-dir -r mcp_server-requirements.txt \
    && pip install --no-cache-dir -r rag-requirements.txt \
    && pip install --no-cache-dir -r backend-requirements.txt \
    && pip install --no-cache-dir python-dotenv

# --- Copy the rest of the application --------------------------------------
COPY . .

# The platform (and the agents it loads) writes to the sqlite DB and to
# rag/qdrant_data (local, on-disk vector store) at runtime.
RUN mkdir -p /app/db /app/rag/qdrant_data

COPY docker/entrypoint.sh /app/docker/entrypoint.sh
RUN chmod +x /app/docker/entrypoint.sh

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=5 \
    CMD curl -f http://localhost:8000/health || exit 1

ENTRYPOINT ["/app/docker/entrypoint.sh"]