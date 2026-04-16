# Stage 1: build the React frontend with Node 20
FROM node:20-slim AS web-builder
RUN apt-get update && \
    apt-get install -y --no-install-recommends git ca-certificates && \
    rm -rf /var/lib/apt/lists/*
RUN git clone --depth 1 https://github.com/NousResearch/hermes-agent.git /hermes-agent
WORKDIR /hermes-agent/web
RUN npm ci && npm run build

# Stage 2: Python runtime
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim
RUN apt-get update && \
    apt-get install -y --no-install-recommends curl ca-certificates && \
    rm -rf /var/lib/apt/lists/*

# Copy the cloned repo (including the built web_dist) from stage 1 and install
COPY --from=web-builder /hermes-agent /tmp/hermes-agent
RUN cd /tmp/hermes-agent && \
    uv pip install --system --no-cache -e ".[all,web]" && \
    rm -rf /tmp/hermes-agent/.git

COPY requirements.txt /app/requirements.txt
RUN uv pip install --system --no-cache -r /app/requirements.txt

# Patch Discord platform: wrap tree.sync() in try/except and truncate descriptions
# to avoid Discord's 8000-byte command-group limit (CommandSyncFailure HTTP 400)
COPY patch_discord.py /tmp/patch_discord.py
RUN python3 /tmp/patch_discord.py

RUN mkdir -p /data/.hermes

COPY server.py /app/server.py
COPY start.sh /app/start.sh
RUN chmod +x /app/start.sh

ENV HOME=/data
ENV HERMES_HOME=/data/.hermes

CMD ["/app/start.sh"]
