# Agent Harness Server in a container (#334 stage e): the service profile, installed from this checkout's wheels.
#
#   docker build -t agent-harness-server .
#   docker run -d --name agent-harness --network host -v agent-harness:/var/lib/agent-harness agent-harness-server
#
# docs/INSTALL.md, "Run the Server in Docker", has the details and what the image leaves out. The build context is the
# allowlist in .dockerignore, so no config, data, token or git history reaches the image; neither does anything a
# wheel leaves out (scripts/packaging/agent_harness_build.py ships .py sources and listed files only).

# The wheels: the in-tree backend needs only the standard library.
FROM python:3.12-slim AS wheels
WORKDIR /src
COPY . .
RUN python scripts/packaging/agent_harness_build.py --out wheels

FROM python:3.12-slim
# git for project workspaces and checkpoints. No Docker client: hosted sessions need Docker access, which the image
# leaves to the owner to grant or not.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*
# The core and every add-on module. A module stays absent until the profile switches it on (HARNESS_ENABLE_MODULES on
# the first start, or config/profile.yaml in the volume).
COPY --from=wheels /src/wheels /tmp/wheels
RUN pip install --no-cache-dir --disable-pip-version-check /tmp/wheels/*.whl \
    && rm -rf /tmp/wheels
# One fixed unprivileged account owns the volume: config/ and data/, with the local owner token the daemon creates on
# its first start.
RUN useradd --system --uid 10001 --user-group --create-home --home-dir /home/harness harness \
    && mkdir -p /var/lib/agent-harness \
    && chown 10001:10001 /var/lib/agent-harness
# XDG_DATA_HOME makes the per-user install's root, $XDG_DATA_HOME/agent-harness, the volume itself, so every location
# the daemon picks by default (the image work and log dirs, the memory library clone, ...) persists with it, not only
# config/ and data/.
ENV HARNESS_CONFIG_DIR=/var/lib/agent-harness/config \
    HARNESS_DATA_DIR=/var/lib/agent-harness/data \
    XDG_DATA_HOME=/var/lib \
    PYTHONUNBUFFERED=1
VOLUME ["/var/lib/agent-harness"]
USER 10001:10001
WORKDIR /home/harness
# The daemon listens on 127.0.0.1 only (harness/container.py); the check runs inside the container, and Docker's
# default number of failed checks marks it unhealthy.
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s \
    CMD ["python", "-m", "harness.container", "--health"]
ENTRYPOINT ["python", "-m", "harness.container"]
