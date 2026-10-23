# Sandbox egress proxy (#671): harness/egress_proxy.py on a pinned slim Python, standard library only. Built by
# ops/egress/compose.yaml with harness/ as the context; the policy file is mounted read-only from ops/egress.
FROM python:3.14-slim@sha256:0514bc76712d64f8bb48adcb95b1ab0d88c69fee95d62fe4c18ee948bee9b1e1
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY egress_proxy.py /opt/egress/egress_proxy.py
USER nobody
EXPOSE 8888
ENTRYPOINT ["python", "-I", "/opt/egress/egress_proxy.py"]
