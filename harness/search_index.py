"""What the session search index holds: which persisted events are indexed, and as what (#334).

The FTS5 table lives in the session database (harness/db.py, so backups carry it) and every event is indexed as it
is written, whether or not the search module is installed; the module (harness_modules/search/) only queries it.
Who may read which session is decided here too, so `session_read` and the taint it passes on (#262) resolve a ref
to the same session (#791).
"""

from __future__ import annotations

from .app_stores import scoped
from .fileops import ToolError

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


def restrict_app_id(db, caller_session_id: str) -> str | None:
    """App id to restrict search/read to, or None if the caller may see every session it can reach.

    Matches `/api/v1`: owner sessions (no app_id) are unrestricted; an app session sees only its
    own sessions unless that app holds `sessions:all` on an unrevoked key. What a caller can reach at all is the
    store of the owner's and members' sessions (Web's, #330 decision 4) plus its own App's (`calling_app`).
    """
    if not caller_session_id:
        return None
    caller = db.get_session(caller_session_id)
    if caller is None:
        raise ToolError(f"no session matches {caller_session_id!r}")
    app_id = caller.get("app_id") or ""
    if not app_id:
        return None
    key = db.get_api_key(app_id)
    if key is None or key.get("revoked_at") is not None:
        return app_id
    scopes = set((key.get("scopes") or "").split())
    if "sessions:all" in scopes:
        return None
    return app_id


def calling_app(db, caller_session_id: str) -> str:
    """The App whose store a session's search and reads may reach besides the main store: its own App's, or none
    for the owner's and members' sessions (#330 decision 3)."""
    caller = db.get_session(caller_session_id) if caller_session_id else None
    return (caller or {}).get("app_id") or ""


def caller_user_id(db, caller_session_id: str) -> str | None:
    """The account whose sessions the caller may search and read, or None (no caller session: every account)."""
    if not caller_session_id:
        return None
    s = db.get_session(caller_session_id)
    return (s.get("owner_id") or "owner") if s else None


def readable_session_ids(db, ref: str, caller_session_id: str) -> list[str]:
    """The agent sessions `session_read` from `caller_session_id` may open whose id starts with `ref`: one means
    that session is the one read. The runner taints the reader from the same lookup (#791)."""
    return scoped(db, calling_app(db, caller_session_id)).find_session_ids(
        ref.strip(), user_id=caller_user_id(db, caller_session_id), app_id=restrict_app_id(db, caller_session_id),
        kind="agent")
