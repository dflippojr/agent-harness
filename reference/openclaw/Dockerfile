FROM node:26-bookworm-slim AS node
FROM agent-harness-sandbox:py312
COPY --from=node /usr/local/ /usr/local/
RUN apt-get update && apt-get install -y --no-install-recommends libatomic1 \
    && rm -rf /var/lib/apt/lists/*
ARG OPENCLAW_VERSION=2026.9.8
LABEL org.opencontainers.image.revision=fc23bc864e4553c2d215e479eeec47b67a0bf943
RUN npm install -g "openclaw@${OPENCLAW_VERSION}"
COPY config.json entrypoint.py /opt/bakeoff/
ENV OPENCLAW_STATE_DIR=/root/.openclaw OPENCLAW_CONFIG_PATH=/root/.openclaw/openclaw.json OPENCLAW_NO_RESPAWN=1
WORKDIR /workspace
