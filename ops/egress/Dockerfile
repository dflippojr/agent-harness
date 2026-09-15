# Allowlist egress proxy for subscription-backend sandboxes (docs/phase8a-design.md). One container per backend,
# each allowing only that provider's domains (filters/<backend>.txt).
FROM alpine:3.20
RUN apk add --no-cache tinyproxy
COPY tinyproxy.conf /etc/tinyproxy/tinyproxy.conf
USER nobody
EXPOSE 8888
ENTRYPOINT ["tinyproxy", "-d", "-c", "/etc/tinyproxy/tinyproxy.conf"]
