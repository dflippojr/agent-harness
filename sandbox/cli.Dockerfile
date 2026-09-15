# Sandbox image for subscription backends (docs/phase8a-design.md): the normal session sandbox plus the unmodified
# Claude Code, Codex and Cursor Agent CLIs. Build after the base image:
#   docker build -t agent-harness-sandbox:py312 sandbox
#   docker build -t agent-harness-cli:1 -f sandbox/cli.Dockerfile sandbox
# Logins live in per-provider volumes mounted at the CLIs' home directories, never in the image.
ARG NODE_IMAGE=node:22-bookworm-slim
FROM ${NODE_IMAGE} AS node

FROM agent-harness-sandbox:py312

ARG CLAUDE_CODE_VERSION=2.1.272
ARG CODEX_VERSION=0.154.0

COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
    && apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl ripgrep \
    && rm -rf /var/lib/apt/lists/*

RUN npm install -g --no-fund --no-audit "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" "@openai/codex@${CODEX_VERSION}" \
    && npm cache clean --force

# The CLIs keep logins and settings in the home directory, which the harness mounts per provider.
RUN useradd --create-home --uid 1000 --shell /bin/bash agent \
    && mkdir -p /home/agent/.claude /home/agent/.codex /home/agent/.cursor /home/agent/.local/bin \
    && chown -R agent:agent /home/agent /workspace
USER agent
ENV HOME=/home/agent PATH=/home/agent/.local/bin:$PATH \
    DISABLE_AUTOUPDATER=1 CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1

# Cursor publishes only an install script (no pinned packages); it installs into ~/.local/bin.
RUN curl -fsS https://cursor.com/install | bash \
    && agent --version

WORKDIR /workspace
