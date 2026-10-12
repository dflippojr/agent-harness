# Sandbox image for subscription backends (docs/phase8a-design.md): the normal session sandbox plus the unmodified
# Claude Code, Codex, Cursor Agent and OpenCode CLIs. Build after the base image:
#   docker build -t agent-harness-sandbox:py312 sandbox
#   docker build -t agent-harness-cli:1 -f sandbox/cli.Dockerfile sandbox
# Logins live in per-provider volumes mounted at the CLIs' home directories, never in the image.
ARG SANDBOX_IMAGE=agent-harness-sandbox:py312
ARG NODE_IMAGE=node:22-bookworm-slim
FROM ${NODE_IMAGE} AS node

FROM ${SANDBOX_IMAGE}

ARG CLAUDE_CODE_VERSION=2.1.272
ARG CODEX_VERSION=0.154.0
# OpenCode (#168) at the version reference/opencode pinned for Phase 0; it runs keyless on the local model.
ARG OPENCODE_VERSION=1.18.30

COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
    && apt-get update \
    && apt-get install -y --no-install-recommends bubblewrap ca-certificates curl ripgrep \
    && rm -rf /var/lib/apt/lists/*

RUN npm install -g --no-fund --no-audit "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" "@openai/codex@${CODEX_VERSION}" \
    && npm cache clean --force

# The CLIs keep logins and settings in the home directory, which the harness mounts per provider.
RUN useradd --create-home --uid 1000 --shell /bin/bash agent \
    && mkdir -p /home/agent/.claude /home/agent/.codex /home/agent/.cursor /home/agent/.local/bin \
    && chown -R agent:agent /home/agent /workspace
USER agent
# NODE_USE_ENV_PROXY: Node's fetch ignores HTTPS_PROXY without it, and the sandbox has no route out except the
# egress proxy (found when `claude auth login` hung exchanging its code).
ENV HOME=/home/agent PATH=/home/agent/.local/bin:$PATH \
    DISABLE_AUTOUPDATER=1 CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1 NODE_USE_ENV_PROXY=1

# Cursor publishes only an install script (no pinned packages); it installs into ~/.local/bin.
RUN curl -fsS https://cursor.com/install | bash \
    && agent --version

# OpenCode installs into ~/.local for the agent user. Its sessions live under XDG_DATA_HOME, which the harness points
# into a per-domain state volume; its config is generated per session and mounted read-only (harness/cli_backends.py).
RUN npm install -g --prefix /home/agent/.local --no-fund --no-audit "opencode-ai@${OPENCODE_VERSION}" \
    && npm cache clean --force \
    && opencode --version

WORKDIR /workspace
