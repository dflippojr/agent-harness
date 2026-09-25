"""Per-account filesystem roots, canonical containment, and quota measurement.

Owner data stays at the historical paths under `data_dir` (workspaces, transcripts) and `repos_dir`.
Member data is rooted at `data_dir/users/<opaque-user-id>/` with separate managed-repository and
workspace/artifact trees. Paths are never derived from a login or display name.

Containment rejects symlinks, Windows junctions/reparse points, traversal, and case/Unicode tricks
that would resolve outside the account root. Quota measurement never follows links.
"""

from __future__ import annotations

import os
import unicodedata
from pathlib import Path

from .fileops import dir_size, resolve_path
from .principal import OWNER_USER_ID

FILE_ATTRIBUTE_REPARSE_POINT = 0x400
INVALID_FILE_ATTRIBUTES = 0xFFFFFFFF


class ContainmentError(ValueError):
    """A path is outside the allowed account root, or is a link that could escape it."""


def user_root(cfg, user_id: str) -> Path:
    if user_id == OWNER_USER_ID:
        return Path(cfg.data_dir)
    if not user_id or user_id != _safe_id(user_id):
        raise ContainmentError("invalid account id")
    return Path(cfg.data_dir) / "users" / user_id


def repos_dir(cfg, user_id: str) -> Path:
    if user_id == OWNER_USER_ID:
        return Path(cfg.repos_dir)
    return user_root(cfg, user_id) / "repos"


def workspaces_dir(cfg, user_id: str) -> Path:
    if user_id == OWNER_USER_ID:
        return Path(cfg.workspaces_dir)
    return user_root(cfg, user_id) / "workspaces"


def transcripts_dir(cfg, user_id: str) -> Path:
    if user_id == OWNER_USER_ID:
        return Path(cfg.transcripts_dir)
    return user_root(cfg, user_id) / "transcripts"


def artifacts_dir(cfg, user_id: str) -> Path:
    if user_id == OWNER_USER_ID:
        return Path(cfg.data_dir) / "artifacts"
    return user_root(cfg, user_id) / "artifacts"


def _safe_id(user_id: str) -> str:
    text = unicodedata.normalize("NFC", (user_id or "").strip())
    if not text or text in (".", "..") or "/" in text or "\\" in text or "\x00" in text:
        return ""
    if any(ord(ch) < 32 for ch in text):
        return ""
    return text


def is_reparse_point(path: Path) -> bool:
    """True for symlinks and Windows junctions / mount points / other reparse points."""
    try:
        if path.is_symlink():
            return True
    except OSError:
        return True
    if os.name != "nt":
        return False
    try:
        import ctypes
        attrs = ctypes.windll.kernel32.GetFileAttributesW(str(path))
        return attrs != INVALID_FILE_ATTRIBUTES and bool(attrs & FILE_ATTRIBUTE_REPARSE_POINT)
    except (AttributeError, OSError, ValueError):
        return False


def _norm_key(path: Path) -> str:
    text = unicodedata.normalize("NFC", str(resolve_path(path)))
    return text.casefold() if os.name == "nt" else text


def _is_within(candidate: Path, root_r: Path) -> bool:
    try:
        return candidate == root_r or candidate.is_relative_to(root_r)
    except (ValueError, OSError):
        return False


def _link_leaves_root(cur: Path, root_r: Path) -> bool:
    """A reparse point whose canonical target is a different path escapes unless that target is still inside root."""
    try:
        if _norm_key(resolve_path(cur)) != _norm_key(cur):
            return not _is_within(resolve_path(cur), root_r)
    except (OSError, RuntimeError, ValueError):
        return True
    return False


def contained(path: Path, root: Path, *, allow_missing: bool = False) -> bool:
    """Whether `path` resolves strictly inside `root` without following a link out."""
    try:
        if not allow_missing and not path.exists() and not path.parent.exists():
            return False
        root_r = resolve_path(root)
        candidate = resolve_path(path)
    except (OSError, RuntimeError):
        return False
    if not _is_within(candidate, root_r):
        return False
    # Walk from candidate up to root: a reparse point anywhere in the chain can escape.
    cur = candidate
    root_key = _norm_key(root_r)
    while True:
        if is_reparse_point(cur) and _norm_key(cur) != root_key and _link_leaves_root(cur, root_r):
            return False
        if _norm_key(cur) == root_key:
            break
        parent = cur.parent
        if parent == cur:
            return False
        cur = parent
    return _norm_key(candidate).startswith(root_key)


def require_contained(path: Path, root: Path, *, allow_missing: bool = True) -> Path:
    """Resolve `path` and raise if it is not inside `root`.

    Symlinks and Windows junctions that resolve outside `root` are refused. Cloud/volume reparse
    points (for example OneDrive) that still resolve inside `root` are allowed.
    """
    try:
        root_r = resolve_path(root)
        candidate = resolve_path(path)
    except (OSError, RuntimeError) as e:
        raise ContainmentError(f"path is not usable: {e}") from e
    if _escapes_via_link(path, root_r) or _escapes_via_link(candidate, root_r):
        raise ContainmentError("linked paths are not allowed outside a checked-in owner tree")
    if not contained(candidate, root_r, allow_missing=allow_missing):
        raise ContainmentError("path escapes the account root")
    return candidate


def _escapes_via_link(path: Path, root: Path) -> bool:
    """True when `path` is a symlink/junction whose target is outside `root`."""
    try:
        if not path.exists():
            return False
        redirecting = path.is_symlink()
        if not redirecting and is_reparse_point(path):
            # Junctions/mount points redirect; cloud placeholders usually resolve in place.
            redirecting = resolve_path(path) != path and not str(resolve_path(path)).casefold().startswith(
                str(path).casefold())
        if not redirecting:
            return False
        target = resolve_path(path)
        return not (target == root or target.is_relative_to(root))
    except (OSError, RuntimeError, ValueError):
        return True


def ensure_user_dirs(cfg, user_id: str) -> Path:
    """Create the member (or owner) storage tree. Never follows an existing link at the root."""
    root = user_root(cfg, user_id)
    if root.exists() and (root.is_symlink() or _escapes_via_link(root, root.parent if root.parent != root else root)):
        raise ContainmentError("account root must not be a link")
    root.mkdir(parents=True, exist_ok=True)
    for sub in (repos_dir(cfg, user_id), workspaces_dir(cfg, user_id), transcripts_dir(cfg, user_id),
                artifacts_dir(cfg, user_id)):
        if sub.exists() and (sub.is_symlink() or _escapes_via_link(sub, root)):
            raise ContainmentError("account storage must not be a link")
        sub.mkdir(parents=True, exist_ok=True)
    return root


def account_usage_bytes(cfg, user_id: str) -> int:
    """Aggregate size of the member root (or owner data dir trees that count toward a quota).

    Does not follow symlinks/junctions. Owner accounts are not quota-capped in v1; this still
    reports the measured size for display.
    """
    if user_id == OWNER_USER_ID:
        total = 0
        for path in (workspaces_dir(cfg, user_id), transcripts_dir(cfg, user_id),
                     artifacts_dir(cfg, user_id), repos_dir(cfg, user_id)):
            if path.is_dir() and not is_reparse_point(path):
                total += dir_size(path)
        return total
    root = user_root(cfg, user_id)
    if not root.is_dir() or is_reparse_point(root):
        return 0
    return dir_size(root)


def quota_message(used: int, limit: int) -> str:
    def fmt(n: int) -> str:
        if n >= 2**30:
            return f"{n / 2**30:.1f} GiB"
        if n >= 2**20:
            return f"{n / 2**20:.1f} MiB"
        return f"{n} B"
    return f"this account is using {fmt(used)} of its {fmt(limit)} disk quota"
