# Sandbox egress proxy (#671): harness/egress_proxy.py on a pinned slim Python, standard library only. Built by
# ops/egress/compose.yaml with harness/ as the context; the policy file is mounted read-only from ops/egress.
FROM python:3.15-slim@sha256:65a65fc7b30b06cb6df4930fd4650697cb23f4c2698ab81fc87e4b031bc36928
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY egress_proxy.py /opt/egress/egress_proxy.py
USER nobody
EXPOSE 8888
ENTRYPOINT ["python", "-I", "/opt/egress/egress_proxy.py"]
