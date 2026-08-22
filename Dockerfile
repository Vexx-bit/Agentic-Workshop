# Cloud Run image: Python + Node (for @playwright/mcp) + Chromium deps.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    NODE_MAJOR=20 \
    BROWSER_HEADLESS=1 \
    BROWSER_ARTIFACT_DIR=/tmp/artifacts

RUN apt-get update && apt-get install -y --no-install-recommends \
        curl ca-certificates gnupg \
    && curl -fsSL https://deb.nodesource.com/setup_${NODE_MAJOR}.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN pip install -r requirements.txt

# Pre-install the MCP server and Chromium so the first request isn't slow.
RUN npm install -g @playwright/mcp@latest playwright \
    && npx playwright install --with-deps chromium

COPY browser_agent ./browser_agent
COPY server ./server

ENV PORT=8080
CMD ["sh", "-c", "uvicorn server.main:app --host 0.0.0.0 --port ${PORT}"]
