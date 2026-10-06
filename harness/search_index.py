"""What the session search index holds: which persisted events are indexed, and as what (#334).

The FTS5 table lives in the session database (harness/db.py, so backups carry it) and every event is indexed as it
is written, whether or not the search module is installed; the module (harness_modules/search/) only queries it.
"""

from __future__ import annotations

SEARCH_TOOLS = ("session_search", "session_read")
INDEX_VERSION = "3"
TOOL_OUTPUT_CHARS = 6000       # indexed prefix of a tool result


_SIMPLE_EVENT_TEXT = {  # event type -> (index kind, data key)
    "session_created": ("title", "title"),
    "user_message": ("message", "content"),
    "app_context": ("context", "content"),
    "error": ("tool", "message"),
}


def _assistant_text(data: dict) -> tuple[str, str] | None:
    parts = [data.get("content") or ""]
    for call in data.get("tool_calls") or []:
        fn = call.get("function") or {}
        parts.append(f"{fn.get('name', '')} {(fn.get('arguments') or '')[:500]}")
    text = "\n".join(p for p in parts if p.strip())
    return ("assistant", text) if text else None


def event_text(type_: str, data: dict) -> tuple[str, str] | None:
    """(kind, text) to index for a persisted event, or None."""
    if type_ == "assistant":
        return _assistant_text(data)
    if type_ == "tool_result":
        if data.get("name") in SEARCH_TOOLS:  # earlier search results would only echo other sessions back
            return None
        return "tool", f"{data.get('name', '')}: {(data.get('output') or '')[:TOOL_OUTPUT_CHARS]}"
    if type_ == "status":
        return ("answer", data["answer"]) if data.get("answer") else None
    simple = _SIMPLE_EVENT_TEXT.get(type_)
    return (simple[0], data.get(simple[1], "")) if simple else None
