"""The real local model (#731): llama-server answers the harness's own client, in text and with a tool call.

Marked `gpu` and skipped unless HARNESS_REAL_MODEL_URL names a running server: CI has none, and the suite never loads
or wakes a model. The real-environment lane (scripts/real_env_lane.py, docs/real-env-lane.md) sets the variable after
checking that the live GPU guard is clear and the model is loaded, then runs `pytest -m gpu`.
"""

from __future__ import annotations

import asyncio
import json
import os

import pytest

from harness import llm
from harness.config import ModelConfig

URL = os.environ.get("HARNESS_REAL_MODEL_URL", "")
pytestmark = [pytest.mark.gpu,
              pytest.mark.skipif(not URL, reason="needs HARNESS_REAL_MODEL_URL (the real-environment lane sets it)")]


def model() -> ModelConfig:
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
