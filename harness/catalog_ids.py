"""Catalog app ids (#518): an optional label on keys and pairing codes naming the listing or Hub entry an App
belongs to. It grants nothing and is never part of a token secret (docs/marketplace-design.md section 8)."""
import re

# docs/marketplace-design.md section 4.2: lowercase reverse-DNS, 2 to 5 labels.
CATALOG_APP_ID = re.compile(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?){1,4}",
                            re.ASCII)
MAX_LENGTH = 120
TOKEN_PREFIXES = ("ha-", "ho-", "hp-", "hk-", "hrp-")


def valid(value) -> bool:
    return (isinstance(value, str) and len(value) <= MAX_LENGTH and not value.startswith(TOKEN_PREFIXES)
            and CATALOG_APP_ID.fullmatch(value) is not None)


def normalize(value) -> str:
    """The stored value: empty when absent, else the id unchanged. Raises ValueError for anything else."""
    if value is None or value == "":
        return ""
    if not isinstance(value, str):
        raise ValueError("catalog_app_id must be a string")
    if len(value) > MAX_LENGTH:
        raise ValueError(f"catalog_app_id must be at most {MAX_LENGTH} characters")
    if value.startswith(TOKEN_PREFIXES):
        raise ValueError("catalog_app_id must not look like a token")
    if not valid(value):
        raise ValueError("catalog_app_id must be a lowercase reverse-DNS id such as com.example.app")
    return value
