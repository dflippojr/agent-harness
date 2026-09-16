"""Checks that quotes in a final answer come from text the agent actually saw.

Local models sometimes give a right answer with a made-up quote attributed to a page they fetched (docs/phase7e).
A quote counts as grounded when it appears in any tool output, the task, a user message or app context. Matching
compares letters and digits only, because PDF text layers break spacing and models add Markdown inside quotes.
"""

from __future__ import annotations

import re
from urllib.parse import quote

MIN_QUOTE_CHARS = 25
_QUOTE = re.compile(r'["“]([^"”\n]{%d,400})["”]' % MIN_QUOTE_CHARS)
_ELLIPSIS = re.compile(r"\.\.\.|…")
_HTTP = re.compile(r"^https?://", re.I)
NUDGE_MARK = "don't appear word for word in anything you read"  # our own request repeats the quotes


def normalize(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def quote_parts(quote_text: str) -> list[str]:
    return [normalize(x) for x in _ELLIPSIS.split(quote_text) if len(normalize(x)) >= 12]


def quote_in(quote_text: str, source: str) -> bool:
    parts = quote_parts(quote_text)
    return bool(parts) and all(part in normalize(source) for part in parts)


def ungrounded_quotes(answer: str, sources: list[str]) -> list[str]:
    """Quoted passages in `answer` that don't appear in any of `sources`. A quote with an ellipsis counts as
    grounded when each part appears."""
    quotes = _QUOTE.findall(answer or "")
    if not quotes:
        return []
    source = normalize("\n".join(sources))
    missing = []
    for q in quotes:
        parts = quote_parts(q)
        if parts and not all(part in source for part in parts):
            missing.append(q)
    return missing


def text_fragment_url(url: str, quote_text: str) -> str:
    """Deep-link to `quote_text` on `url` (`#:~:text=`). Chrome/Edge follow it; Safari/iOS support is partial."""
    base = (url or "").split("#", 1)[0]
    snippet = re.sub(r"\s+", " ", quote_text).strip()[:80]
    return f"{base}#:~:text={quote(snippet, safe='')}"


def quote_hrefs(answer: str, pages: list[tuple[str, str]]) -> dict[str, str]:
    """Map quoted passages in `answer` to the first `web_fetch` URL they appear in, as text-fragment links."""
    hrefs = {}
    web_pages = [(url, text) for url, text in pages if _HTTP.match(url or "")]
    for q in _QUOTE.findall(answer or ""):
        for url, text in web_pages:
            if quote_in(q, text):
                hrefs[q] = text_fragment_url(url, q)
                break
    return hrefs


def session_sources(context: list[dict], events: list[dict]) -> list[str]:
    """Everything the agent was given: current context messages (full tool output, the task, app context) plus
    every recorded tool result, which still covers messages that compaction has since summarized."""
    texts = [m["content"] for m in context if m.get("role") != "assistant" and isinstance(m.get("content"), str)]
    for e in events:
        if e["type"] in ("tool_result", "app_tool_result", "user_message", "app_context"):
            data = e.get("data") or {}
            texts.extend(v for k, v in data.items() if k in ("output", "content", "text") and isinstance(v, str))
            if isinstance(data.get("context"), list):
                texts.extend(str(block.get("content", "")) for block in data["context"] if isinstance(block, dict))
    return [text for text in texts if NUDGE_MARK not in text]


def nudge(quotes: list[str]) -> str:
    listed = "\n".join(f'- "{q}"' for q in quotes)
    return (f"Before you finish: these quotes in your answer {NUDGE_MARK} during this task:\n{listed}\n"
            "Fix each one: copy the exact wording from the source, paraphrase without quotation marks, or remove it. "
            "Then give your final answer again.")
