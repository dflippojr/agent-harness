# Per-session sandbox image. Agents only ever see /workspace inside this container.
FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends git procps \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir "pytest==8.*" pyyaml

# Bind-mounted workspaces from Windows show up with foreign ownership.
RUN git config --system safe.directory '*' \
    && git config --system user.name "Agent" \
    && git config --system user.email "agent@localhost" \
    && git config --system init.defaultBranch main

WORKDIR /workspace
