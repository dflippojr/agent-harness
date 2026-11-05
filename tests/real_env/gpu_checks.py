"""The real local model (#731): llama-server answers the harness's own client, in text and with a tool call.

Not part of the CI suite: the file name keeps pytest's default collection away from it (CI has no GPU), so it is
never a skipped test there either. The real-environment lane (scripts/real_env_lane.py, docs/real-env-lane.md) runs
it by path, `pytest -m gpu tests/real_env/gpu_checks.py`, after checking that the live GPU guard is clear and the
model is loaded, with HARNESS_REAL_MODEL_URL naming the server. Without that variable every check fails.
"""

from __future__ import annotations

import asyncio
import json
import os

import pytest

from harness import llm
from harness.config import ModelConfig

URL = os.environ.get("HARNESS_REAL_MODEL_URL", "")
pytestmark = pytest.mark.gpu


def model() -> ModelConfig:
    assert URL, "HARNESS_REAL_MODEL_URL must name the real llama-server (the real-environment lane sets it)"
    return ModelConfig(name=os.environ.get("HARNESS_REAL_MODEL_NAME") or "local", base_url=URL.rstrip("/"),
                       context_tokens=8192, max_tokens=2048)


def test_real_model_answers_in_text():
    out = asyncio.run(llm.chat(model(), [{"role": "user", "content": "Reply with exactly the word pong."}],
                               timeout=300))
    assert "pong" in out.content.lower(), out
    assert out.completion_tokens > 0 and out.prompt_tokens > 0


def test_real_model_calls_a_tool_in_the_harness_format():
    tool = {"type": "function", "function": {
        "name": "write_file", "description": "Write a text file in the workspace.",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                       "required": ["path", "content"]}}}
    messages = [{"role": "user", "content": "Call write_file once to create lane.txt containing real-env-ok."}]
    out = asyncio.run(llm.chat(model(), messages, tools=[tool], timeout=300))
    assert out.tool_calls, out
    call = out.tool_calls[0]
    assert call["function"]["name"] == "write_file"
    args = json.loads(call["function"]["arguments"])
    assert args["path"].endswith("lane.txt") and "real-env-ok" in args["content"]
