# Pi coding agent (#942) on top of the bake-off sandbox image, so Pi sees the same Linux tools (Python, pytest, git)
# as the native driver's sessions and the hidden checkers. `python -m bakeoff.canary --driver pi` builds it and runs
# it with no network; socat relays its loopback port to the run's socket to llama-server (bakeoff/pi_driver.py).
# Pi 1.5.0 needs Node >= 22.19 (package.json engines); Debian trixie ships 20, so Node comes from the official image.
FROM node:24.22.2-trixie-slim AS pi
# Keep in step with bakeoff/pi_driver.py's PI_VERSION.
ARG PI_VERSION=1.5.0
RUN npm install -g --ignore-scripts "@earendil-works/pi-coding-agent@${PI_VERSION}"

FROM agent-harness-sandbox:py312
LABEL org.opencontainers.image.source=https://github.com/earendil-works/pi
# earendil-works/pi tag v1.5.0
LABEL org.opencontainers.image.revision=f9a26365568127461c47a07a37dcb42c140a3ac5
RUN apt-get update \
    && apt-get install -y --no-install-recommends ripgrep socat \
    && rm -rf /var/lib/apt/lists/*
COPY --from=pi /usr/local/bin/node /usr/local/bin/node
COPY --from=pi /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s /usr/local/lib/node_modules/@earendil-works/pi-coding-agent/dist/bundle/cli.js /usr/local/bin/pi \
    && pi --version
COPY entrypoint.sh /opt/bakeoff/entrypoint.sh
ENTRYPOINT ["/opt/bakeoff/entrypoint.sh"]
WORKDIR /workspace
