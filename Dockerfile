FROM mcr.microsoft.com/playwright:v1.55.0-noble

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_BREAK_SYSTEM_PACKAGES=1 \
    BROWSER_HEADLESS=1 \
    BROWSER_ARTIFACT_DIR=/tmp/artifacts \
    PORT=8080

RUN apt-get update \
    && apt-get install -y --no-install-recommends python3.12 python3-pip \
    && ln -sf /usr/bin/python3.12 /usr/local/bin/python \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN python -m pip install -r requirements.txt

# Pre-install the MCP server so the first WhatsApp message isn't slowed by npx.
RUN npm install -g @playwright/mcp@latest

COPY browser_agent ./browser_agent
COPY server ./server

CMD ["sh", "-c", "python -m uvicorn server.main:app --host 0.0.0.0 --port ${PORT}"]
