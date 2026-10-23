"""Experiment (#641, docs/compaction.md): re-inject the saved plan on every native model call.

With `planning.reinject` on, each call to a local agent session's model gets the `plan` and `next_step` from its saved
`update_state` object appended to the end of the last message, in a copy of the request only. The stored context, the
transcript and the events never hold it, and nothing is inserted above the newest turn, so the prompt prefix the
model server caches stays the same from one call to the next.
"""

from __future__ import annotations

import copy

from . import state as agent_state

MARKER = "[current plan, from update_state; not part of the conversation]"
MAX_CHARS = 1200
ELLIPSIS = "\n…\n"
# Added to a native agent session's system prompt when the flag is on, so the prompt stays fixed for the session.
PROMPT = ("Each request ends with your current plan and next step from `update_state`; keep them current by calling "
          "`update_state` whenever your plan changes.")


def plan_text(saved) -> str:
    """The plan and next step of a schema-valid saved state, at most MAX_CHARS long; empty when there is none."""
    if not agent_state.is_valid_state(saved):
        return ""
    parts = []
    if (saved.get("plan") or "").strip():
        parts.append(f"Plan:\n{saved['plan'].strip()}")
    if (saved.get("next_step") or "").strip():
        parts.append(f"Next step: {saved['next_step'].strip()}")
    text = "\n\n".join(parts)
    if len(text) <= MAX_CHARS:
        return text
    keep = MAX_CHARS - len(ELLIPSIS)
    head = keep - keep // 2  # the plan's start and the next step at the end both survive the cut
    return text[:head] + ELLIPSIS + text[len(text) - keep // 2:]


def block(text: str) -> str:
    return f"\n\n{MARKER}\n{text}"


def with_plan(messages: list[dict], saved) -> list[dict]:
    """`messages` itself when there is no plan to add; otherwise a deep copy whose last message ends with it."""
    text = plan_text(saved)
    if not text or not messages:
        return messages
    content = messages[-1].get("content")
    if not isinstance(content, (str, list)):
        return messages
    out = copy.deepcopy(messages)
    last = out[-1]
    if isinstance(content, str):
        last["content"] = content + block(text)
    else:  # OpenAI content parts: one more text part, still the same message and role
        last["content"].append({"type": "text", "text": block(text).lstrip("\n")})
    return out


def strip(text: str) -> str:
    """`text` without a plan block appended to it (the fake model matches scripts on the message as stored). A content
    part list reaches here joined with spaces, so its block follows a space instead of a blank line."""
    at = text.rfind(MARKER + "\n")
    if at < 0:
        return text
    return text[:at].removesuffix("\n\n").removesuffix(" ")
