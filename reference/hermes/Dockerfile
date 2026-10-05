FROM agent-harness-sandbox:py312
ARG HERMES_REVISION=f97608f178d1ffeca59860195ab7da295f7c8e5f
LABEL org.opencontainers.image.revision=f97608f178d1ffeca59860195ab7da295f7c8e5f
RUN git clone https://github.com/NousResearch/hermes-agent /opt/hermes \
    && cd /opt/hermes && git checkout "${HERMES_REVISION}" \
    && pip install --no-cache-dir -e .
COPY config.json entrypoint.py /opt/bakeoff/
ENV HERMES_HOME=/root/.hermes HERMES_SKIP_SETUP=1
WORKDIR /workspace
