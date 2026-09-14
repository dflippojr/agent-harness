# OpenHands CLI on top of the bake-off sandbox image, so the agent sees the same Linux tools (Python 3.12, pytest,
# git) as the baseline harness. The container runs on an internal network whose only route out is the LLM proxy.
FROM agent-harness-sandbox:py312

COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /uvx /usr/local/bin/

ENV UV_TOOL_DIR=/opt/uv-tools \
    UV_TOOL_BIN_DIR=/usr/local/bin \
    UV_PYTHON_INSTALL_DIR=/opt/uv-python

ENV OPENHANDS_SUPPRESS_BANNER=1 \
    LITELLM_LOCAL_MODEL_COST_MAP=True

ARG OPENHANDS_VERSION=1.16.0
RUN uv tool install "openhands==${OPENHANDS_VERSION}" --python 3.12 \
    && openhands --version

# OpenHands clones its public skills on startup. The run network has no internet, so bake them in at build time
# to give the agent its normal skill set.
RUN git clone --depth 1 --branch main https://github.com/OpenHands/extensions /root/.openhands/cache/skills/public-skills

WORKDIR /workspace
