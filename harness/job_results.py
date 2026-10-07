"""Shared scheduled-session result protocol for the runner and notifications."""

import re

_STATUS_HEAD = re.compile(r"STATUS:\s*(OK|ATTENTION)\b", re.IGNORECASE)


def _line_start_before(text: str, k: int, floor: int) -> int | None:
    """The first line start at or after floor from which only non-word characters lead up to k, if any."""
    r = k
    while r > floor and not (text[r - 1].isalnum() or text[r - 1] == "_"):
        r -= 1
    if r == 0 or text[r - 1] == "\n":
        return r
    newline = text.find("\n", r, k)
    return None if newline < 0 else newline + 1


def _status_lines(text: str):
    """(start, end, verdict, reason) for each STATUS line, the matches of the multiline, case-insensitive
    `^\\W*STATUS:\\s*(OK|ATTENTION)\\b[:\\s-]*(.*)$`. Scanned by hand: that regex retries a run of non-word
    characters from every line start inside it, which is quadratic."""
    pos = floor = 0
    while m := _STATUS_HEAD.search(text, pos):
        start = _line_start_before(text, m.start(), floor)
        if start is None:
            pos = m.start() + 1
            continue
        s = m.end()
        while s < len(text) and (text[s] in ":-" or text[s].isspace()):
            s += 1
        end = text.find("\n", s)
        end = len(text) if end < 0 else end
        yield start, end, m.group(1), text[s:end]
        pos = floor = end


def parse_status(answer: str) -> tuple[str, str]:
    """('ok' | 'attention' | '', reason) from a job's final answer. The last STATUS line wins."""
    matches = list(_status_lines(answer or ""))
    if not matches:
        return "", ""
    _, _, verdict, reason = matches[-1]
    return verdict.lower(), reason.strip().rstrip("*_` ").strip()


def _strip_status_lines(text: str) -> str:
    parts, pos = [], 0
    for start, end, _, _ in _status_lines(text):
        parts.append(text[pos:start])
        pos = end
    parts.append(text[pos:])
    return "".join(parts)


def summary(answer: str, limit: int = 300) -> str:
    """A notification-sized summary of a job's answer: the last prose paragraph before the STATUS line (agents
    usually put their verdict there), skipping tables, headings and code."""
    text = _strip_status_lines(answer or "").strip()
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    for p in reversed(paragraphs):
        lines = [line for line in p.splitlines() if not re.match(r"\s*(\||#|```|---)", line)]
        prose = " ".join(" ".join(lines).split())
        if len(prose) >= 20:
            return prose if len(prose) <= limit else prose[: limit - 1] + "…"
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"
