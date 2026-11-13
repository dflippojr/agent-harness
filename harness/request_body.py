"""Reading a JSON request body by hand (#930, #965).

`json.loads` reports most malformed bodies as `ValueError` (bad syntax, invalid UTF-8, an integer too long to
convert), but a deeply nested body such as `[[[...]]]` exhausts the parser's recursion limit and raises
`RecursionError` instead. A route that catches only `ValueError` turns that body into a 500. Every request-body parse
catches `BODY_PARSE_ERRORS`; tests/test_request_body_parse.py audits the source for it."""
from __future__ import annotations

from typing import Any

from fastapi import Request

BODY_PARSE_ERRORS: tuple[type[Exception], ...] = (ValueError, RecursionError)


async def json_body(request: Request) -> Any:
    """The parsed JSON body, or None when the body is not JSON. A caller that needs an object checks the type."""
    try:
        return await request.json()
    except BODY_PARSE_ERRORS:
        return None
