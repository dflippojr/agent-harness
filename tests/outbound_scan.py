"""Find every outbound network call site in the shipped code (#733).

A call site is the function or method that constructs a network client or opens a connection: `httpx.Client`,
`httpx.get`, `urllib.request.urlopen`, `socket.create_connection` and the rest of NETWORK_CALLS. It is named
`path::Qualified.name`, the same key tests/outbound_registry.py uses, so a new integration fails
test_outbound_failures.py until its failure paths are registered.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCANNED = ("harness", "harness_modules", "sdk")

# module -> names whose call opens a connection or builds a client that will. "*" is every name in the module.
NETWORK_CALLS = {
    "httpx": {"Client", "AsyncClient", "get", "post", "put", "patch", "delete", "head", "options", "request",
              "stream"},
    "requests": {"*"},
    "aiohttp": {"ClientSession", "request"},
    "urllib.request": {"urlopen", "build_opener", "OpenerDirector"},
    "http.client": {"HTTPConnection", "HTTPSConnection"},
    "websockets": {"connect"},
    "websockets.client": {"connect"},
    "socket": {"create_connection"},
    "asyncio": {"open_connection"},
    "smtplib": {"SMTP", "SMTP_SSL"},
    "ftplib": {"FTP", "FTP_TLS"},
    "opentelemetry.exporter.otlp.proto.http.trace_exporter": {"OTLPSpanExporter"},
    "opentelemetry.exporter.otlp.proto.grpc.trace_exporter": {"OTLPSpanExporter"},
}


def _matches(module: str, name: str) -> bool:
    names = NETWORK_CALLS.get(module)
    return names is not None and ("*" in names or name in names)


class _Visitor(ast.NodeVisitor):
    def __init__(self, path: str):
        self.path = path
        self.scope: list[str] = []
        self.modules: dict[str, str] = {}   # local alias -> module ("h" -> "httpx" for `import httpx as h`)
        self.names: dict[str, tuple[str, str]] = {}  # local name -> (module, name) for `from m import n`
        self.sites: dict[str, list[int]] = {}

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            if alias.asname:
                self.modules[alias.asname] = alias.name
            else:  # `import urllib.request` binds `urllib`; the dotted chain is resolved at the call
                self.modules[alias.name.split(".")[0]] = alias.name.split(".")[0]

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        for alias in node.names:
            local = alias.asname or alias.name
            if node.module and f"{node.module}.{alias.name}" in NETWORK_CALLS:  # from urllib import request
                self.modules[local] = f"{node.module}.{alias.name}"
            elif node.module:
                self.names[local] = (node.module, alias.name)

    def _scoped(self, node, name: str) -> None:
        self.scope.append(name)
        self.generic_visit(node)
        self.scope.pop()

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._scoped(node, node.name)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._scoped(node, node.name)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._scoped(node, node.name)

    def _target(self, func: ast.expr) -> tuple[str, str] | None:
        chain = []
        while isinstance(func, ast.Attribute):
            chain.append(func.attr)
            func = func.value
        if not isinstance(func, ast.Name):
            return None
        chain.reverse()
        if not chain:
            return self.names.get(func.id)
        if func.id not in self.modules:
            return None
        return ".".join([self.modules[func.id], *chain[:-1]]), chain[-1]

    def visit_Call(self, node: ast.Call) -> None:
        target = self._target(node.func)
        if target and _matches(*target):
            site = f"{self.path}::{'.'.join(self.scope) or '<module>'}"
            self.sites.setdefault(site, []).append(node.lineno)
        self.generic_visit(node)


def scan(root: Path = ROOT) -> dict[str, list[int]]:
    """{site: [line numbers]} for every network call in the scanned packages."""
    sites: dict[str, list[int]] = {}
    for package in SCANNED:
        for file in sorted((root / package).rglob("*.py")):
            rel = file.relative_to(root).as_posix()
            visitor = _Visitor(rel)
            visitor.visit(ast.parse(file.read_text(encoding="utf-8"), filename=rel))
            sites.update(visitor.sites)
    return sites


if __name__ == "__main__":
    for site, lines in sorted(scan().items()):
        print(site, lines)
