# OpenHands CLI on top of the bake-off sandbox image, so the agent sees the same Linux tools (Python 3.12, pytest,
# git) as the baseline harness. The container runs on an internal network whose only route out is the LLM proxy.
FROM agent-harness-sandbox:py312

COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /uvx /usr/local/bin/

ENV UV_TOOL_DIR=/opt/uv-tools \
    UV_TOOL_BIN_DIR=/usr/local/bin \
    UV_PYTHON_INSTALL_DIR=/opt/uv-python

ENV OPENHANDS_SUPPRESS_BANNER=1 \
    LITELLM_LOCAL_MODEL_COST_MAP=True

# Latest release is still 1.16.0 at stage A's 2026-10-04 compatibility check.
ARG OPENHANDS_VERSION=1.16.0
LABEL org.opencontainers.image.revision=2963442dacc7cea44e39b7c4e73724295c853465
RUN uv tool install "openhands==${OPENHANDS_VERSION}" --python 3.12 \
    && openhands --version

# OpenHands clones its public skills on startup. The run network has no internet, so bake them in at build time
# to give the agent its normal skill set.
ARG OPENHANDS_SKILLS_REVISION=3ed88619ff2b4bccdf95a88f1dbc6e7182bf365d
RUN git clone https://github.com/OpenHands/extensions /root/.openhands/cache/skills/public-skills \
    && cd /root/.openhands/cache/skills/public-skills && git checkout "${OPENHANDS_SKILLS_REVISION}"

WORKDIR /workspace
