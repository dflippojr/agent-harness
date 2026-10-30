"""One SHA-256 hex digest of text (#841): the hash stored for keys, codes and tickets, so the mint and lookup paths
for one token hash identically."""
from __future__ import annotations

import hashlib


def sha256_hex(text: str) -> str:
    """The SHA-256 hex digest of `text` encoded as UTF-8. Callers truncate (`[:16]`) where they need to."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
