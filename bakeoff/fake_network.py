"""Own an isolated Docker network and a scripted model container, never a shared proxy."""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path

import httpx

from . import reference


class FakeEndpoint:
    def __init__(self, script: Path, artifacts: Path):
        self.script = script.resolve()
        self.artifacts = artifacts.resolve()
        token = uuid.uuid4().hex[:12]
        self.network = f"bakeoff-fake-{token}"
        self.name = f"bakeoff-model-{token}"
        self.port = None
        self.previous = reference.NETWORK, reference.PROXY

    def __enter__(self):
        self.artifacts.mkdir(parents=True, exist_ok=True)
        reference.docker("network", "create", "--internal", self.network)
        try:
            # Like the existing socat proxy: the endpoint publishes a loopback
            # port on the default bridge, then joins the internal agent network.
            # Candidate containers only join the internal network.
            reference.docker("run", "-d", "--rm", "--name", self.name,
                             "--publish", "127.0.0.1::8080", "--memory", "128m", "--cpus", "1",
                             "--mount", f"type=bind,source={Path(__file__).with_name('fake_endpoint.py')},target=/fake.py,readonly",
                             "--mount", f"type=bind,source={self.script},target=/script.json,readonly",
                             "--mount", f"type=bind,source={self.artifacts},target=/artifacts",
                             "agent-harness-sandbox:py312", "python", "/fake.py", "--script", "/script.json",
                             "--requests", "/artifacts/requests.jsonl")
            reference.docker("network", "connect", self.network, self.name)
            ports = json.loads(reference.docker("inspect", self.name, "--format", "{{json .NetworkSettings.Ports}}").stdout)
            self.port = int(ports["8080/tcp"][0]["HostPort"])
            reference.NETWORK, reference.PROXY = self.network, self.name
            deadline = time.monotonic() + 15
            while True:
                try:
                    with httpx.Client(trust_env=False) as client:
                        client.get(f"http://127.0.0.1:{self.port}/health", timeout=1).raise_for_status()
                    break
                except httpx.HTTPError:
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(.1)
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    @property
    def container_port(self):
        return 8080

    def __exit__(self, *args):
        reference.NETWORK, reference.PROXY = self.previous
        reference.docker("rm", "-f", self.name, check=False)
        reference.docker("network", "rm", self.network, check=False)
