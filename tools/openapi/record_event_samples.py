"""Refresh tests/fixtures/sse_event_samples.json: real SSE event payloads, recorded from the Server's test run (#1014).

    HARNESS_RECORD_SSE_EVENTS=.tmp/sse-events python -m pytest tests -q -n 4 --dist loadfile
    python tools/openapi/record_event_samples.py .tmp/sse-events             rewrite the samples
    python tools/openapi/record_event_samples.py .tmp/sse-events --shapes    print each type's fields instead

The test run's hook (tests/sse_event_capture.py) appends the wire form of every event the Server's own code emitted.
This keeps a few of each type: per set of fields seen, the shortest payload, at most `PER_TYPE` per type, so the
samples cover a type's optional fields. Paths under the checkout and the home folder are rewritten so no machine's layout is committed.
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SAMPLES = ROOT / "tests" / "fixtures" / "sse_event_samples.json"
PER_TYPE = 4


def _scrub(text: str) -> str:
    """The checkout's and the home folder's paths, as the JSON line spells them, become placeholders."""
    for path, placeholder in ((str(ROOT), "/checkout"), (str(Path.home()), "/home/user")):
        if path not in ("", "/"):
            text = text.replace(json.dumps(path)[1:-1], placeholder)
    return text


def load(folder: Path) -> list[dict]:
    events = []
    for path in sorted(folder.glob("events-*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                events.append(json.loads(_scrub(line)))
    return events


def _fields(value, prefix: str = "") -> set[str]:
    """Dotted field names, one level into nested objects, so a sample per optional field is kept."""
    out = set()
    if isinstance(value, dict):
        for key, item in value.items():
            out.add(prefix + key)
            if not prefix and isinstance(item, dict):
                out |= _fields(item, key + ".")
    return out


def samples(events: list[dict]) -> dict[str, list[dict]]:
    by_shape: dict[str, dict[frozenset, dict]] = defaultdict(dict)
    for event in events:
        shape = frozenset(_fields(event))
        best = by_shape[event["type"]].get(shape)
        text = json.dumps(event, sort_keys=True)
        if best is None or (len(text), text) < (len(json.dumps(best, sort_keys=True)), json.dumps(best, sort_keys=True)):
            by_shape[event["type"]][shape] = event
    out = {}
    for type_, shapes in sorted(by_shape.items()):
        # The widest shape first (most optional fields), then the narrowest, then the rest by size.
        ordered = sorted(shapes.items(), key=lambda item: (-len(item[0]), json.dumps(item[1], sort_keys=True)))
        keep = [ordered[0]]
        rest = ordered[1:]
        if rest:
            narrow = min(rest, key=lambda item: (len(item[0]), json.dumps(item[1], sort_keys=True)))
            keep.append(narrow)
            rest = [item for item in rest if item is not narrow]
        keep += sorted(rest, key=lambda item: len(json.dumps(item[1])))[:PER_TYPE - len(keep)]
        out[type_] = [event for _, event in keep]
    return out


def shapes(events: list[dict]) -> str:
    seen: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    counts: dict[str, int] = defaultdict(int)
    present: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for event in events:
        counts[event["type"]] += 1
        for key, value in event.items():
            if key != "data":
                seen[event["type"]]["@" + key].add(type(value).__name__)
                present[event["type"]]["@" + key] += 1
        for key, value in (event.get("data") or {}).items():
            seen[event["type"]][key].add(type(value).__name__)
            present[event["type"]][key] += 1
            if isinstance(value, dict):
                for sub, item in value.items():
                    seen[event["type"]][f"{key}.{sub}"].add(type(item).__name__)
                    present[event["type"]][f"{key}.{sub}"] += 1
    lines = []
    for type_ in sorted(seen):
        lines.append(f"{type_} ({counts[type_]})")
        for key in sorted(seen[type_]):
            lines.append(f"  {key}: {'|'.join(sorted(seen[type_][key]))} [{present[type_][key]}/{counts[type_]}]")
    return "\n".join(lines) + "\n"


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__, file=sys.stderr)
        return 2
    events = load(Path(argv[0]))
    if "--shapes" in argv:
        sys.stdout.write(shapes(events))
        return 0
    text = json.dumps(samples(events), indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    SAMPLES.write_text(text, encoding="utf-8", newline="\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
