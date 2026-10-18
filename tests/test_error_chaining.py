"""Error paths that the #729 ruff fixes (B904) now chain with `from e`: each keeps its status and message and records
the original error as the cause, so a traceback still shows what failed underneath."""

from __future__ import annotations

import asyncio
import re
import zipfile
from types import SimpleNamespace

import httpx
import pytest

from harness import admin, api, pairing_requests, projects
from harness.config import WebConfig
from harness.fileops import ToolError
from harness.manager import HarnessError
from harness.web_tools import WebTools, docx_to_text
from harness_modules.search.service import find_passages

from test_phase4 import harness_runner

PUBLIC = "93.184.216.34"
NOT_AN_ORIGIN = "ftp://example.com/path"


async def _public(host, port):
    return [PUBLIC]


@pytest.mark.parametrize("call,status", [
    (lambda: admin._normalized_origin(NOT_AN_ORIGIN), 403),
    (lambda: api._key_origins({"origins": [NOT_AN_ORIGIN]}, "owner"), 400),
    (lambda: pairing_requests._request_origin(SimpleNamespace(headers={"origin": NOT_AN_ORIGIN})), 403),
])
def test_a_bad_origin_keeps_its_status_and_chains_the_value_error(call, status):
    with pytest.raises(HarnessError) as caught:
        call()
    assert caught.value.status == status
    assert "origin must contain only" in str(caught.value)
    assert isinstance(caught.value.__cause__, ValueError)


def test_a_bad_find_pattern_chains_the_regex_error():
    with pytest.raises(ToolError, match="bad find pattern") as caught:
        find_passages("some text", "(")
    assert isinstance(caught.value.__cause__, re.error)


def test_a_document_that_is_not_a_zip_chains_the_zip_error():
    with pytest.raises(ToolError, match="couldn't open the Word document") as caught:
        docx_to_text(b"not a zip archive")
    assert isinstance(caught.value.__cause__, zipfile.BadZipFile)


def test_a_host_that_does_not_resolve_chains_the_os_error():
    async def unresolvable(host, port):
        raise OSError("name or service not known")

    web = WebTools(WebConfig(enabled=True), resolver=unresolvable, transport=httpx.MockTransport(lambda r: None))
    with pytest.raises(ToolError, match="can't resolve example.com") as caught:
        asyncio.run(web.web_fetch("https://example.com/page"))
    assert isinstance(caught.value.__cause__, OSError)


def test_a_failed_download_chains_the_http_error():
    def refuse(request):
        raise httpx.ConnectError("connection refused", request=request)

    web = WebTools(WebConfig(enabled=True), resolver=_public, transport=httpx.MockTransport(refuse))
    with pytest.raises(ToolError, match="fetch failed: ConnectError") as caught:
        asyncio.run(web.web_fetch("https://example.com/page"))
    assert isinstance(caught.value.__cause__, httpx.ConnectError)


def test_a_bad_find_pattern_on_a_fetched_page_chains_the_regex_error():
    web = WebTools(WebConfig(enabled=True), resolver=_public,
                   transport=httpx.MockTransport(lambda r: httpx.Response(200, text="plain page text")))
    with pytest.raises(ToolError, match="bad find pattern") as caught:
        asyncio.run(web.web_fetch("https://example.com/page", find="("))
    assert isinstance(caught.value.__cause__, re.error)


def test_a_runner_git_failure_is_a_git_op_error_with_its_cause():
    def op_status(params):
        raise projects.GitError("not a git repository")

    with pytest.raises(harness_runner.OpError, match="not a git repository") as caught:
        harness_runner.Executor.handle(SimpleNamespace(op_status=op_status), "rid", "status", {})
    assert caught.value.kind == "git"
    assert isinstance(caught.value.__cause__, projects.GitError)
