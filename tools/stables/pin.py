"""Pin the Stables release this repository deploys and tests against (#334).

Stables, the browser client, lives in dflippojr/stables. This directory records the release the Server uses:

- `pin.json`: the repository, tag and commit. The tower deploy (`ops/harness/deploy-ci.ps1`) serves that commit, and
  the Playwright journeys (`tools/ui-tests`) run against it.
- `api-calls.json`: every `api(...)` call in that release's `public/`, plus its browser storage keys. The
  management-parity test (`tests/test_management_parity.py`) reads it instead of the client's source.

    python tools/stables/pin.py <stables checkout>           pin the checkout's tagged commit
    python tools/stables/pin.py --check <stables checkout>   fail unless the checkout is the pinned commit and the
                                                             call list matches it
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PIN = HERE / "pin.json"
CALLS = HERE / "api-calls.json"
REPOSITORY = "dflippojr/stables"


def _skip_string(text: str, i: int) -> int:
    """Index just past the string literal opening at text[i] (template literals may nest `${...}`)."""
    quote, i = text[i], i + 1
    while i < len(text) and text[i] != quote:
        if text[i] == "\\":
            i += 2
            continue
        if quote == "`" and text.startswith("${", i):
            i = _skip_expression(text, i + 2, "}")
        i += 1
    return i + 1


def _skip_expression(text: str, i: int, close: str) -> int:
    """Index of the `close` that ends the expression starting at text[i], skipping nested brackets and strings."""
    depth = 0
    while i < len(text):
        ch = text[i]
        if ch in "\"'`":
            i = _skip_string(text, i)
            continue
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            if depth == 0:
                return i
            depth -= 1
        elif ch == "," and depth == 0 and close == ",":
            return i
        i += 1
    return i


def path_patterns(expr: str) -> list[tuple[str, str]]:
    """(regex, sample) for each path literal in an api() first argument. In the regex `${...}` matches one segment
    (or any suffix when it follows other text); in the sample it is `x`, for matching against `{param}` rows."""
    patterns, i = [], 0
    while i < len(expr):
        if expr[i] not in "\"'`":
            i += 1
            continue
        end = _skip_string(expr, i)
        body, i = expr[i + 1:end - 1], end
        if body and not body.startswith("/"):
            continue
        out, sample, j = "", "", 0
        while j < len(body):
            if body.startswith("${", j):
                k = _skip_expression(body, j + 2, "}")
                out += "[^/?]+" if out.endswith("/") else "(?:/[^?]*)?"
                sample += "x"
                j = k + 1
            else:
                out += re.escape(body[j])
                sample += body[j]
                j += 1
        patterns.append((out.split(r"\?")[0] or "/", sample.split("?")[0] or "/"))
    return patterns


def call_methods(options: str) -> list[str] | None:
    """Methods named in an api() options object: GET when there is no `method`, None when it's not a literal."""
    found = re.search(r"\bmethod\b\s*(:)?", options)
    if not found:
        return ["GET"]
    if not found.group(1):
        return None
    start = found.end()
    expr = options[start:_skip_expression(options, start, ",")]
    verbs = sorted(set(re.findall(r"[\"'](GET|POST|PUT|PATCH|DELETE)[\"']", expr)))
    return verbs or None


def api_calls(text: str):
    """(line, path expression, options text) for each `api(...)` or `agentHarnessWeb.request(...)` call in `text`."""
    for m in re.finditer(r"(?<![\w.])api\(|agentHarnessWeb\.request\(", text):
        start = m.end()
        first_end = _skip_expression(text, start, ",")
        close = _skip_expression(text, start, ")")
        options = text[first_end:close] if first_end < close else ""
        yield text.count("\n", 0, m.start()) + 1, text[start:min(first_end, close)], options


def _sources(checkout: Path) -> list[Path]:
    public = checkout / "public"
    return sorted([*public.rglob("*.mjs"), public / "app.js"])


def _snippet_languages(text: str) -> dict:
    """`SNIPPET_LANGUAGES` in lib/snippets.mjs: {id: {"label", "aliases"}}."""
    block = text[text.index("export const SNIPPET_LANGUAGES"):]
    block = block[:block.index("};") + 2]
    return {m[1]: {"label": m[2], "aliases": sorted(re.findall(r'"([^"]+)"', m[3]))}
            for m in re.finditer(r'(?<!\w)(\w+): \{ label: "([^"]+)", aliases: \[([^\]]*)\]', block)}


def calls_in(text: str, where: str = "") -> list[dict]:
    """A record per path in each api() call: where, path regex, sample, methods, and whether it uses the App API."""
    return [{"where": f"{where}:{line}", "pattern": pattern, "sample": sample, "methods": call_methods(options),
             "app_surface": bool(re.search(r"(?<!\w)surface\s*:\s*[\"']app[\"']", options))}
            for line, expr, options in api_calls(text) for pattern, sample in path_patterns(expr)]


def paths_in(text: str, where: str = "") -> list[dict]:
    """A record per path literal (a string starting with `/`), with `${...}` as `x` and no query."""
    return [{"where": f"{where}:{text.count(chr(10), 0, m.start()) + 1}",
             "sample": re.sub(r"\$\{[^}]*\}", "x", m.group(1)).split("?")[0]}
            for m in re.finditer(r"[\"'`](/[^\"'`\s]*)[\"'`]", text)]


def call_list(checkout: Path) -> dict:
    """What the Server's tests check about the client, as `api-calls.json` records it: each api() call (with whether
    it goes over the App API), every path literal, the browser storage keys, the build id and the snippet languages."""
    calls, paths, keys = [], [], set()
    for path in _sources(checkout):
        text = path.read_text(encoding="utf-8")
        rel = path.relative_to(checkout).as_posix()
        keys |= set(re.findall(r"[\"'`](harness\.[A-Za-z]+)[\"'`]", text))
        calls += calls_in(text, rel)
        paths += paths_in(text, rel)
    public = checkout / "public"
    build = re.search(r'WEB_BUILD_ID = "([^"]+)"', (public / "client.mjs").read_text(encoding="utf-8"))
    return {"build_id": build[1] if build else None,
            "snippet_languages": _snippet_languages((public / "lib" / "snippets.mjs").read_text(encoding="utf-8")),
            "browser_keys": sorted(keys), "calls": calls, "path_literals": paths, "sources": len(_sources(checkout))}


def _git(checkout: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(checkout), *args], capture_output=True, text=True,
                          check=True).stdout.strip()


def _dump(value: dict) -> str:
    return json.dumps(value, indent=2) + "\n"


def pin(checkout: Path) -> dict:
    """Pin the checkout's commit, which must carry a release tag."""
    commit = _git(checkout, "rev-parse", "HEAD")
    tags = sorted(t for t in _git(checkout, "tag", "--points-at", "HEAD").split() if re.fullmatch(r"v\d+\.\d+\.\d+", t))
    if not tags:
        raise SystemExit(f"{checkout} is not at a release tag (vX.Y.Z); tag the release first")
    record = {"repository": REPOSITORY, "tag": tags[-1], "commit": commit}
    PIN.write_text(_dump(record), encoding="utf-8", newline="\n")
    CALLS.write_text(_dump(call_list(checkout)), encoding="utf-8", newline="\n")
    return record


def check(checkout: Path) -> list[str]:
    """What is wrong with the pin for this checkout; empty when the checkout is the pinned commit and matches."""
    pinned = json.loads(PIN.read_text(encoding="utf-8"))
    problems = []
    commit = _git(checkout, "rev-parse", "HEAD")
    if commit != pinned["commit"]:
        problems.append(f"{checkout} is at {commit}, but pin.json pins {pinned['tag']} at {pinned['commit']}")
    if CALLS.read_text(encoding="utf-8") != _dump(call_list(checkout)):
        problems.append("api-calls.json does not match the checkout: run python tools/stables/pin.py <checkout>")
    return problems


def main(argv: list[str]) -> int:
    args = [a for a in argv if not a.startswith("--")]
    if len(args) != 1:
        print(__doc__, file=sys.stderr)
        return 2
    checkout = Path(args[0]).resolve()
    if "--check" in argv:
        problems = check(checkout)
        for problem in problems:
            print(problem, file=sys.stderr)
        return 1 if problems else 0
    record = pin(checkout)
    print(f"pinned {record['repository']} {record['tag']} at {record['commit']}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
