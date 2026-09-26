"""Recorded web for reproducible runs of web_search / web_fetch (Phase 7e).

A fixture is a directory of real SearXNG answers and raw page bytes (HTML, PDF, ...), captured once:

    python -m harness.web_fixture record D:/Agents/harness/web-fixture \\
        --query "llama.cpp server sleep idle seconds" --url https://arxiv.org/pdf/1706.03762 --fetch-top 3

With `web.fixture_dir` set, WebTools replays it instead of touching the network: searches return the recorded results
(an unrecorded query gets the recorded query that shares the most words, or nothing), and fetches return the recorded
bytes, which still go through the normal extraction (trafilatura, PDF, paging, find). Benchmarks then see the same
web every run (bakeoff/web_suite.py).

Fixtures hold third-party content, so they live outside the repository (default under the data directory).
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlparse, urlsplit, urlunsplit

import httpx

FIXTURE_IP = "93.184.216.34"   # any public address: replayed fetches never connect anywhere
SEARXNG_HOSTS = ("127.0.0.1", "localhost", "::1")  # the recorder asks the SearXNG on this machine, nothing else


def normalize_query(query: str) -> str:
    return " ".join(re.findall(r"\w+", query.lower()))


def normalize_url(url: str) -> str:
    parts = urlsplit(url.strip())
    path = parts.path or "/"
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, parts.query, ""))


def _key(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


class Fixture:
    def __init__(self, root: Path | str):
        self.root = Path(root)
        self.manifest_path = self.root / "manifest.json"
        self.manifest = (json.loads(self.manifest_path.read_text(encoding="utf-8")) if self.manifest_path.exists()
                         else {"searches": {}, "pages": {}})
        self.misses: list[str] = []   # what a replay asked for that wasn't recorded

    def save(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.manifest["updated_at"] = time.time()
        self.manifest_path.write_text(json.dumps(self.manifest, indent=1, sort_keys=True), encoding="utf-8")

    # recording
    def add_search(self, query: str, data: dict) -> None:
        name = f"searches/{_key(normalize_query(query))}.json"
        (self.root / "searches").mkdir(parents=True, exist_ok=True)
        (self.root / name).write_text(json.dumps(data), encoding="utf-8")
        self.manifest["searches"][normalize_query(query)] = name

    def add_page(self, url: str, final: str, content_type: str, body: bytes) -> None:
        name = f"pages/{_key(normalize_url(final))}.bin"
        (self.root / "pages").mkdir(parents=True, exist_ok=True)
        (self.root / name).write_bytes(body)
        self.manifest["pages"][normalize_url(final)] = {"file": name, "content_type": content_type,
                                                        "bytes": len(body)}
        if normalize_url(url) != normalize_url(final):
            self.manifest["pages"][normalize_url(url)] = {"redirect": final}

    # replay
    async def resolve(self, _host: str, _port: int) -> list[str]:  # underscore names: WebTools resolver signature
        return [FIXTURE_IP]

    def search(self, query: str) -> dict:
        wanted = normalize_query(query)
        name = self.manifest["searches"].get(wanted)
        if name is None:
            words = set(wanted.split())
            best, score = None, 0.0
            for recorded, file in self.manifest["searches"].items():
                have = set(recorded.split())
                overlap = len(words & have) / max(1, len(words | have))
                if overlap > score:
                    best, score = file, overlap
            if best is None or score < 0.25:
                self.misses.append(f"search: {query}")
                return {"results": [], "query": query}
            name = best
        return json.loads((self.root / name).read_text(encoding="utf-8"))

    def handler(self, request: httpx.Request) -> httpx.Response:
        host = request.headers.get("host", request.url.netloc.decode() if isinstance(request.url.netloc, bytes)
                                   else str(request.url.netloc))
        if request.url.path == "/search" and "q" in request.url.params and request.url.host in ("127.0.0.1", "localhost"):
            return httpx.Response(200, json=self.search(request.url.params["q"]))
        url = normalize_url(urlunsplit((request.url.scheme, host, request.url.path, request.url.query.decode()
                                        if isinstance(request.url.query, bytes) else str(request.url.query), "")))
        entry = self.manifest["pages"].get(url) or self.manifest["pages"].get(url.rstrip("/"))
        if entry is None and not url.endswith("/"):
            entry = self.manifest["pages"].get(url + "/")
        if entry is None:
            self.misses.append(f"fetch: {url}")
            return httpx.Response(404, text="not in the web fixture")
        if "redirect" in entry:
            return httpx.Response(302, headers={"location": entry["redirect"]})
        return httpx.Response(200, content=(self.root / entry["file"]).read_bytes(),
                              headers={"content-type": entry["content_type"] or "application/octet-stream"})

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


def searxng_search_url(searxng_url: str) -> str:
    """The /search endpoint of the local SearXNG at `searxng_url`. Any other host, a scheme other than http(s),
    credentials, a query or fragment, or a bad port is a ValueError, so the recorder can't be pointed elsewhere."""
    parts = urlparse(searxng_url.strip())
    if parts.scheme not in ("http", "https") or parts.hostname not in SEARXNG_HOSTS:
        raise ValueError(f"the SearXNG URL must be http(s) on {', '.join(SEARXNG_HOSTS)}, not {searxng_url!r}")
    if parts.username is not None or parts.password is not None or parts.params or parts.query or parts.fragment:
        raise ValueError(f"the SearXNG URL must be a plain base URL, not {searxng_url!r}")
    try:
        port = parts.port
    except ValueError as e:
        raise ValueError(f"the SearXNG URL has a bad port: {searxng_url!r}") from e
    host = f"[{parts.hostname}]" if ":" in parts.hostname else parts.hostname
    netloc = host if port is None else f"{host}:{port}"
    return f"{parts.scheme}://{netloc}{parts.path.rstrip('/')}/search"


async def record(root: Path, queries: list[str], urls: list[str], fetch_top: int, searxng_url: str) -> Fixture:
    from .config import WebConfig
    from .web_tools import WebTools
    search_url = searxng_search_url(searxng_url)
    fixture = Fixture(root)
    web = WebTools(WebConfig(enabled=True, searxng_url=searxng_url))
    to_fetch = list(urls)
    async with httpx.AsyncClient(timeout=30, trust_env=False) as client:
        for q in queries:
            resp = await client.get(search_url, params={"q": q, "format": "json", "pageno": 1})
            resp.raise_for_status()
            data = resp.json()
            results = sorted(data.get("results") or [], key=lambda r: -float(r.get("score") or 0))
            fixture.add_search(q, {"results": results[:20]})
            print(f"search {q!r}: {len(results)} results")
            to_fetch += [r["url"] for r in results[:fetch_top] if r.get("url")]
    from .web_tools import github_sources
    expanded = []
    for url in to_fetch:  # GitHub pages are read through the API and raw host (web_tools.github_sources)
        info = github_sources(url)
        expanded += list(info["urls"].values()) if info else []
        expanded.append(url)  # the HTML too, for the fallback
    for url in dict.fromkeys(expanded):
        try:
            final, ctype, body = await web._download(url)
        except Exception as e:  # noqa: BLE001 - record what can be recorded
            print(f"  skip {url}: {e}")
            continue
        fixture.add_page(url, final, ctype, body)
        print(f"  page {url} -> {ctype}, {len(body)} bytes")
    fixture.save()
    return fixture


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m harness.web_fixture")
    sub = parser.add_subparsers(dest="command", required=True)
    rec = sub.add_parser("record", help="record live searches and pages into a fixture directory")
    rec.add_argument("dir")
    rec.add_argument("--query", action="append", default=[])
    rec.add_argument("--url", action="append", default=[])
    rec.add_argument("--fetch-top", type=int, default=3, help="also record the top N results of every query")
    rec.add_argument("--searxng-url", default="http://127.0.0.1:8888")
    show = sub.add_parser("show", help="list what a fixture contains")
    show.add_argument("dir")
    args = parser.parse_args(argv)
    if args.command == "record":
        try:
            searxng_search_url(args.searxng_url)
        except ValueError as e:
            parser.error(str(e))
        asyncio.run(record(Path(args.dir), args.query, args.url, args.fetch_top, args.searxng_url))
    else:
        f = Fixture(args.dir)
        for q in sorted(f.manifest["searches"]):
            print(f"search  {q}")
        for u, e in sorted(f.manifest["pages"].items()):
            print(f"page    {u}  " + (f"-> {e['redirect']}" if "redirect" in e else f"{e['content_type']} {e['bytes']}"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
