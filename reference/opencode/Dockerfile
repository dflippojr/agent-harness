# OpenCode CLI on top of the bake-off sandbox image, so the agent sees the same Linux tools (Python 3.12, pytest,
# git) as the baseline harness. Like the OpenHands image, it runs on the internal `harness-llm` network whose only
# route out is the LLM proxy.
FROM agent-harness-sandbox:py312

RUN apt-get update \
    && apt-get install -y --no-install-recommends nodejs npm ripgrep \
    && rm -rf /var/lib/apt/lists/*

ARG OPENCODE_VERSION=1.18.30
RUN npm install -g "opencode-ai@${OPENCODE_VERSION}" && opencode --version

COPY opencode.json /root/.config/opencode/opencode.json

WORKDIR /workspace
