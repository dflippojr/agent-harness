"""Path and URL confinement for bakeoff/compare_qwen_lightning.py."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "bakeoff" / "compare_qwen_lightning.py"


def load_compare():
    spec = importlib.util.spec_from_file_location("compare_qwen_lightning", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def test_confine_out_dir_rejects_escape(tmp_path):
    cmp = load_compare()
    inside = cmp.confine_out_dir("docs/issue-13-outputs", root=tmp_path)
    assert inside == (tmp_path / "docs" / "issue-13-outputs").resolve()
    assert inside.is_relative_to(tmp_path.resolve())
    with pytest.raises(ValueError, match="relative path"):
        cmp.confine_out_dir("../outside", root=tmp_path)
    with pytest.raises(ValueError, match="relative path"):
        cmp.confine_out_dir(str(tmp_path / "abs"), root=tmp_path)
    nested = cmp.confine_output_file(inside, "summary.json")
    assert nested == (inside / "summary.json").resolve()
    with pytest.raises(ValueError, match="refusing output name"):
        cmp.confine_output_file(inside, "../secret.json")


def test_daemon_url_and_job_id_reject_traversal():
    cmp = load_compare()
    assert cmp.daemon_base("http://127.0.0.1:8100/") == "http://127.0.0.1:8100"
    assert cmp.daemon_url("http://127.0.0.1:8100", "images", "abc123def456") == \
        "http://127.0.0.1:8100/images/abc123def456"
    with pytest.raises(ValueError, match="must be http://127.0.0.1"):
        cmp.daemon_base("http://evil.example/images")
    with pytest.raises(ValueError, match="must not include a path"):
        cmp.daemon_base("http://127.0.0.1:8100/images/../etc")
    with pytest.raises(ValueError, match="invalid image job id"):
        cmp.sanitize_job_id("../etc/passwd")
    with pytest.raises(ValueError, match="invalid image job id"):
        cmp.sanitize_job_id("abc/../def")
    with pytest.raises(ValueError, match="invalid prompt id"):
        cmp.sanitize_prompt_id("..")
    with pytest.raises(ValueError, match="refusing URL path"):
        cmp.request("http://127.0.0.1:8100/images/../secret")
