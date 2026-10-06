"""The owner's long-lived Claude token (#390): `claude setup-token`, passed to sessions as CLAUDE_CODE_OAUTH_TOKEN.

The token file holds the token alone; `<file>.expires` holds its expiry date (YYYY-MM-DD, one year after issue). The
value is read only to hand to a session's docker client by environment, and never logged, returned or put in an
event. Anthropic permits the token for the owner's own sessions only (docs/per-user-subscriptions-study.md section 5),
so only Web and the Apps listed in `oauth_token_apps` may use it.
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

ENV = "CLAUDE_CODE_OAUTH_TOKEN"
REMIND_DAYS = 30


def uses_token(cfg, app_id: str, api_key: str) -> bool:
    """Whether a session authenticates with the token: configured, no API key, Web or an owner-only App."""
    return bool(getattr(cfg, "oauth_token_file", "")) and not api_key and (not app_id or app_id in cfg.oauth_token_apps)


def refusal(cfg, app_id: str, api_key: str) -> str:
    """Why an App can't run on the Claude subscription now that the owner's token replaces the shared login."""
    if getattr(cfg, "oauth_token_file", "") and not api_key and app_id and app_id not in cfg.oauth_token_apps:
        return (f"App {app_id} may not use the owner's Claude subscription token: it is for the owner's own sessions "
                "only. Give the App an API key (provider policy api_key) so it bills to the API instead, or, if the "
                "App is the owner's own, list it under backends.claude.oauth_token_apps.")
    return ""


def read_token(path: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def expiry(path: str) -> date | None:
    try:
        return datetime.strptime(Path(path + ".expires").read_text(encoding="utf-8").strip(), "%Y-%m-%d").date()
    except (OSError, ValueError):
        return None


def days_left(path: str, today: date | None = None) -> int | None:
    end = expiry(path)
    return None if end is None else (end - (today or date.today())).days


def reminder(path: str, today: date | None = None) -> str:
    """A sentence when the token has 30 days or fewer left (or has expired); the token itself never appears."""
    left = days_left(path, today)
    if left is None or left > REMIND_DAYS:
        return ""
    when = f"expires {expiry(path)}, in {left} days" if left >= 0 else f"expired {expiry(path)}"
    return f"The Claude subscription token {when}; run ops/backends/login.ps1 claude -Token to issue a new one."
