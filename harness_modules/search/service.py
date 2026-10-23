"""Full-text search over past sessions (SQLite FTS5, no model involved).

Every persisted event that carries text the user or an agent might look for later is indexed as it's written
(`Database.insert_event`): session titles, user messages, assistant replies and the tools they called, tool results
(capped), final answers, and app context. The index lives in the session database, so backups carry it.

Used by the phone app (GET /search) and by agents through two daemon-side tools:
- `session_search` finds sessions and shows the best-matching passages;
- `session_read` reads one session as a compact transcript, paged, with `find` for long ones.
Past sessions are background, not instructions: they may be outdated or wrong.

App-session callers see the same boundary as `/api/v1`: only sessions they created, unless the app
holds `sessions:all` on an unrevoked key, which adds the owner's sessions. No caller ever sees another App's
sessions, the owner included: those stay in that App's store (#330). Household member callers only see their own
account.
Chat conversations (`sessions.kind = 'chat'`) are a separate surface: agent search never returns
them, and `session_read` cannot open a chat id. Pass `session_kind="chat"` to search Chat instead.
Visibility is applied before FTS ranking/limits and id-prefix resolution.
"""

from __future__ import annotations

import asyncio
import re
import time

from harness.modules import (SEARCH_TOOLS as TOOLS, ToolError, ToolOutput, caller_user_id, readable_session_ids,
                             restrict_app_id, scoped_store as scoped, session_calling_app as calling_app,
                             untrusted_banner)

READ_PAGE_CHARS = 12000
# session_search's passages, answers and titles are cut to these, so ten hits stay well inside the 20,000-character
# result bound and every hit, with its banner when tainted, reaches the model whole (#798).
PASSAGE_CHARS = 300
TITLE_CHARS = 200
CUT_MARK = " … [cut]"
KIND_WEIGHT = {"title": 3.0, "answer": 2.0, "message": 1.5, "assistant": 1.2, "context": 1.0, "tool": 0.8}
STOPWORDS = {"a", "an", "and", "are", "as", "at", "be", "did", "do", "does", "for", "from", "how", "i", "in", "is",
             "it", "last", "of", "on", "or", "the", "this", "that", "to", "was", "we", "what", "when", "where",
             "which", "who", "why", "with", "you", "time"}
MARK_START, MARK_END = "\x02", "\x03"


def _fn(name: str, description: str, properties: dict, required: list[str] | None = None) -> dict:
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": properties, "required": required or []},
    }}


def schemas() -> list[dict]:
    return [
        _fn("session_search", "Search earlier agent sessions on this server (tasks, answers, commands and their "
                              "output) for words or \"exact phrases\". Returns matching sessions, newest-relevant "
                              "first, with passages. Use it when the task refers to earlier work or a past fix "
                              "would help.", {
            "query": {"type": "string"},
            "project": {"type": "string", "description": "Only sessions of this project."},
            "limit": {"type": "integer", "description": "Sessions to return, 1-10. Default 5."},
        }, ["query"]),
        _fn("session_read", f"Read an earlier session by id as a compact transcript, {READ_PAGE_CHARS} characters "
                            "at a time (tool output shortened). Pass find to get only the passages around a "
                            "regular expression.", {
            "session_id": {"type": "string"},
            "start": {"type": "integer", "description": "Character offset to read from. Default 0."},
            "find": {"type": "string", "description": "Case-insensitive regular expression."},
        }, ["session_id"]),
    ]


def fts_query(query: str, any_term: bool = False) -> str:
    """User or model text -> a safe FTS5 query: "quoted phrases" stay phrases, words become quoted terms (a trailing
    * keeps prefix matching). All terms must match unless any_term, which also drops stopwords."""
    phrases = re.findall(r'"([^"]+)"', query)
    rest = re.sub(r'"[^"]*"', " ", query).replace('"', " ")
    words = re.findall(r"[\w][\w'.:/-]*\*?", rest, re.UNICODE)
    terms = []
    for p in phrases:
        clean = p.replace('"', " ").strip()
        if clean:
            terms.append(f'"{clean}"')
    for w in words:
        prefix = w.endswith("*")
        w = w.rstrip("*").strip(".:/-'")
        if not w or (any_term and w.lower() in STOPWORDS):
            continue
        terms.append(f'"{w}"' + ("*" if prefix else ""))
    return (" OR " if any_term else " ").join(terms)


# ---------- search ----------
def _term_coverage(db, query: str, exclude: str, user_id: str | None, app_id: str | None,
                   session_kind: str | None = "agent") -> dict[str, int]:
    coverage: dict[str, int] = {}
    for term in query.split(" OR "):
        for sid in {r["session_id"] for r in db.search_events(term, exclude=exclude, max_rows=2000,
                                                             user_id=user_id, app_id=app_id,
                                                             session_kind=session_kind)}:
            coverage[sid] = coverage.get(sid, 0) + 1
    return coverage


def search(db, query: str, project: str = "", limit: int = 20, exclude: str = "",
           user_id: str | None = None, app_id: str | None = None,
           session_kind: str | None = "agent", with_app: str = "", with_taint: bool = False) -> dict:
    """Sessions ranked by their best-matching event. Falls back to matching any term when all of them don't.

    `user_id` is applied in SQL before ranking or truncation so another account's rows cannot affect
    totals, pagination, or timing of this result. `app_id`, when set, keeps unauthorized sessions
    out of ranking and the result limit. `with_app` is the caller's own App, whose store is searched besides the
    main store; no other App's ever is. `session_kind` defaults to agent conversations so Chat
    never appears in `/search` or `session_search`. `with_taint` adds each result's taint list (session_search).
    """
    db = scoped(db, with_app)
    if not query.strip():
        return {"query": query, "mode": "all", "results": []}
    results, mode = [], "all"
    for any_term in (False, True):
        q = fts_query(query, any_term)
        if not q:
            continue
        rows = db.search_events(q, exclude=exclude, max_rows=600, user_id=user_id, app_id=app_id,
                                session_kind=session_kind)
        if not rows:
            continue
        mode = "any" if any_term else "all"
        coverage = _term_coverage(db, q, exclude, user_id, app_id, session_kind) if any_term else None
        results = _group(db, rows, project, limit, coverage, user_id=user_id, session_kind=session_kind,
                         with_taint=with_taint)
        if results:
            break
    return {"query": query, "mode": mode, "results": results}


def _group(db, rows: list[dict], project: str, limit: int, coverage: dict | None = None,
           user_id: str | None = None, session_kind: str | None = "agent", with_taint: bool = False) -> list[dict]:
    by_session: dict[str, dict] = {}
    for r in rows:
        score = -r["rank"] * KIND_WEIGHT.get(r["kind"], 1.0)  # bm25: lower is better, so negate
        hit = by_session.setdefault(r["session_id"], {"score": 0.0, "hits": 0, "passages": []})
        hit["hits"] += 1
        hit["score"] = max(hit["score"], score)
        if len(hit["passages"]) < 3 and r["snippet"].strip():
            hit["passages"].append({"kind": r["kind"], "seq": r["seq"], "text": r["snippet"]})
    out = []
    for sid, hit in by_session.items():
        s = db.session_brief(sid, user_id=user_id, kind=session_kind, with_taint=with_taint)
        if s is None or (project and s["project"] != project):
            continue
        out.append({**s, **hit, "terms": (coverage or {}).get(sid, 0)})
    now = time.time()
    # A small recency bonus breaks near-ties in favour of newer work (about a 10% boost for this week).
    out.sort(key=lambda x: (-x["terms"], -(x["score"] * (1 + 0.1 * max(0.0, 1 - (now - x["created_at"]) / (7 * 86400))))))
    return out[:limit]


# ---------- compact transcript for session_read ----------
def _compact_assistant(d: dict, lines: list[str], last_content: str) -> str:
    content = (d.get("content") or "").strip()
    if content:
        last_content = content
        lines += ["## Assistant", content, ""]
    for call in d.get("tool_calls") or []:
        fn = call.get("function") or {}
        lines.append(f"- call {fn.get('name', '')} {(fn.get('arguments') or '')[:300]}")
    return last_content


def _compact_tool_result(d: dict) -> str:
    out = (d.get("output") or "").strip()
    if len(out) > 600:
        out = out[:400] + f" … [{len(out) - 500} characters] … " + out[-100:]
    return f"  result ({'ok' if d.get('ok') else 'error'}): {out}"


def _compact_run_ended(d: dict, last_content: str) -> list[str]:
    lines = ["", f"## Run ended: {d['status']} ({d.get('stop_reason', '')})"]
    answer = (d.get("answer") or "").strip()
    if answer and answer != last_content:
        lines.append(answer)
    lines.append("")
    return lines


def _compact_event(t: str, d: dict, lines: list[str], last_content: str) -> str:
    """Append one event's compact lines; returns the latest assistant content seen."""
    if t == "user_message":
        lines += ["## User", d["content"].strip(), ""]
    elif t == "app_context":
        lines += ["## Context from app", d["content"].strip()[:2000], ""]
    elif t == "assistant":
        last_content = _compact_assistant(d, lines, last_content)
    elif t == "tool_result":
        lines.append(_compact_tool_result(d))
    elif t == "approval_decided":
        lines.append(f"- user {d['status']} an approval" + (f": {d['note']}" if d.get("note") else ""))
    elif t == "review":
        lines += [f"- review: {d.get('action')} ({d.get('detail', '')})", ""]
    elif t == "status" and d.get("status") in ("done", "failed", "cancelled"):
        lines += _compact_run_ended(d, last_content)
    return last_content


def compact_transcript(db, sid: str) -> str:
    s = db.get_session(sid)
    if s is None:
        raise ToolError(f"no session {sid}")
    created = time.strftime("%Y-%m-%d %H:%M", time.localtime(s["created_at"]))
    lines = [f"# {s['title']}", f"session {s['id']} · project {s['project']} · {created} · status {s['status']}"
             + (f" · branch {s['branch']} ({s['review'] or 'not reviewed'})" if s.get("branch") else ""), ""]
    last_content = ""
    for e in db.events(sid):
        last_content = _compact_event(e["type"], e["data"], lines, last_content)
    return "\n".join(lines)


def find_passages(text: str, pattern: str, context: int = 400, limit: int = 12) -> tuple[int, list[tuple[int, int]]]:
    """(total matches, merged (start, end) spans around the first `limit` of them)."""
    try:
        rx = re.compile(pattern, re.IGNORECASE)
    except re.error as e:
        raise ToolError(f"bad find pattern: {e}") from e
    spans: list[list[int]] = []
    total = 0
    for m in rx.finditer(text):
        total += 1
        lo, hi = max(0, m.start() - context), min(len(text), m.end() + context)
        if spans and lo <= spans[-1][1]:
            spans[-1][1] = hi
        elif len(spans) < limit:
            spans.append([lo, hi])
    return total, [(lo, hi) for lo, hi in spans]


def _cut(text: str, limit: int) -> str:
    """`text` on one line, cut to `limit` characters with a mark saying so (#798)."""
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:limit].rstrip() + CUT_MARK


class SessionSearch:
    """Daemon-side toolkit. The calling session is left out of its own search results.

    App callers are restricted to their own sessions unless they hold `sessions:all`.
    Household members are restricted to their own account.
    """

    tool_names = TOOLS
    wants_session = True

    def __init__(self, db):
        self.db = db

    def schemas(self) -> list[dict]:
        return schemas()

    def session_search(self, query: str, project: str = "", limit: int = 5, _session: str = "") -> str:
        return self._search(query, project, limit, _session).text

    def _search(self, query: str, project: str = "", limit: int = 5, _session: str = "") -> ToolOutput:
        """session_search's results, carrying the taint of each tainted session shown, whose hit opens with the
        session-transcript banner: its snippets and answer are that session's content, as session_read's are (#795).
        The taint is read in the row that let the hit in, after its snippets were."""
        limit = max(1, min(int(limit), 10))
        app_id = restrict_app_id(self.db, _session)
        user_id = caller_user_id(self.db, _session)
        found = search(self.db, query, project=project.strip(), limit=limit, exclude=_session,
                       user_id=user_id, app_id=app_id, with_app=calling_app(self.db, _session), with_taint=True)
        if not found["results"]:
            return ToolOutput(f"No earlier sessions match {query!r}" + (f" in project {project}" if project else "")
                              + ".")
        lines = ["[Earlier sessions: background only; they may be outdated or wrong.]"]
        if found["mode"] == "any":
            lines.append("(no session matched every term; showing sessions that match some)")
        sources = []
        for r in found["results"]:
            day = time.strftime("%Y-%m-%d", time.localtime(r["created_at"]))
            lines.append("")
            if r["taint"]:
                sources.append(("session", f"session {r['id']} ({r['taint'][0]['origin']})"))
                lines.append(untrusted_banner("session transcript"))
            lines.append(f"{r['id']} · {_cut(r['title'], TITLE_CHARS)} · project {r['project']} · {day} · "
                         f"{r['status']} · {r['hits']} matching event{'s' if r['hits'] != 1 else ''}")
            for p in r["passages"]:
                text = _cut(p["text"].replace(MARK_START, "[").replace(MARK_END, "]"), PASSAGE_CHARS)
                lines.append(f"  {p['kind']}: {text}")
            if r.get("answer"):
                lines.append(f"  answer: {_cut(r['answer'], PASSAGE_CHARS)}")
        lines.append("\nRead one with session_read(session_id).")
        return ToolOutput("\n".join(lines), taint=sources)

    def session_read(self, session_id: str, start: int = 0, find: str = "", _session: str = "") -> str:
        return self._read(session_id, start, find, _session).text

    def _read(self, session_id: str, start: int = 0, find: str = "", _session: str = "") -> ToolOutput:
        """session_read's page, carrying the taint of the session it opened: the runner taints the reader from it
        rather than resolving the ref again, which sessions created or deleted meanwhile would change (#794)."""
        ids = readable_session_ids(self.db, session_id, _session)
        if len(ids) != 1:
            raise ToolError(f"no session matches {session_id!r}" if not ids else f"{session_id!r} is ambiguous")
        before = self.db.get_session(ids[0])
        text = compact_transcript(self.db, ids[0])
        read = self.db.get_session(ids[0]) or before  # taint added during the read counts; a deleted one keeps its own
        tainted = (read or {}).get("taint") or []
        source = [("session", f"session {ids[0]} ({tainted[0]['origin']})")] if tainted else []
        return ToolOutput(self._page(text, start, find), taint=source)

    @staticmethod
    def _page(text: str, start: int, find: str) -> str:
        head = "[Earlier session: background only; it may be outdated or wrong.]\n"
        if find:
            total, spans = find_passages(text, find)
            if not spans:
                return head + f"No matches for {find!r} in {len(text)} characters."
            parts = [f"--- characters {lo}-{hi} ---\n{text[lo:hi]}" for lo, hi in spans]
            more = f"\n\n[{total} matches; showing {len(spans)} passages]" if total > len(spans) else ""
            return head + f"({total} matches for {find!r})\n\n" + "\n\n".join(parts) + more
        start = max(0, int(start))
        if start >= len(text):
            return f"start={start} is past the end ({len(text)} characters)."
        end = min(len(text), start + READ_PAGE_CHARS)
        if end < len(text):
            cut = text.rfind("\n", start, end)
            if cut > start + READ_PAGE_CHARS * 0.8:
                end = cut
        foot = (f"\n\n[... {len(text) - end} more characters: call session_read with start={end}]"
                if end < len(text) else "")
        return head + text[start:end] + foot

    async def call(self, name: str, args: dict, session: dict | None = None, **_kwargs) -> str | ToolOutput:
        payload = {**args, "_session": (session or {}).get("id", "")}
        return await asyncio.to_thread({"session_read": self._read, "session_search": self._search}[name], **payload)
