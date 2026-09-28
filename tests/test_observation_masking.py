import hashlib

from harness import compaction
from harness.db import Database


def _turn(tool_id="c1", name="read_file", content="x" * 2000):
    return [
        {"role": "assistant", "content": None, "tool_calls": [{"id": tool_id, "function": {
            "name": name, "arguments": '{"path":"a.txt"}'}}]},
        {"role": "tool", "tool_call_id": tool_id, "content": content},
        {"role": "assistant", "content": "I used the result."},
    ]


def test_mask_threshold_boundary_and_receipt_artifact():
    content = "x" * 2000
    masked, artifacts, saved = compaction.mask_used_results(
        _turn(content=content), {"c1": {"ok": True, "name": "read_file"}}, 2000)
    assert masked[1]["content"].startswith("[Observation receipt]")
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    assert digest in masked[1]["content"]
    assert artifacts == {digest: content}
    assert saved > 0

    unchanged, artifacts, saved = compaction.mask_used_results(
        _turn(content=content[:-1]), {"c1": {"ok": True}}, 2000)
    assert unchanged[1]["content"] == content[:-1]
    assert not artifacts and saved == 0


def test_only_older_successful_results_are_masked_and_read_results_are_exempt():
    messages = _turn()
    messages.extend([
        {"role": "assistant", "content": None, "tool_calls": [{"id": "c2", "function": {
            "name": "run_shell", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c2", "content": "y" * 2000},
    ])
    masked, artifacts, _ = compaction.mask_used_results(
        messages, {"c1": {"ok": True}, "c2": {"ok": True}}, 2000)
    assert masked[1]["content"].startswith("[Observation receipt]")
    assert masked[-1]["content"] == "y" * 2000
    assert len(artifacts) == 1

    recovered, artifacts, _ = compaction.mask_used_results(
        _turn(name="read_artifact"), {"c1": {"ok": True}}, 2000)
    assert recovered[1]["content"] == "x" * 2000
    assert not artifacts


def test_failures_and_unknown_legacy_outcomes_stay_verbatim():
    messages = _turn()
    for outcomes in ({"c1": {"ok": False}}, {}):
        unchanged, artifacts, _ = compaction.mask_used_results(messages, outcomes, 2000)
        assert unchanged[1]["content"] == "x" * 2000
        assert not artifacts


def test_artifact_full_text_ranges_hash_and_resume(tmp_path):
    text = "😀" * 22001
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    path = tmp_path / "harness.sqlite3"
    db = Database(path)
    db.put_artifact("session-a", digest, text)
    db.close()

    db = Database(path)
    first, truncated = db.read_artifact("session-a", digest)
    assert first == text[:20000]
    assert truncated
    rest, truncated = db.read_artifact("session-a", digest, 20000, len(text))
    assert rest == text[20000:]
    assert not truncated
    assert hashlib.sha256((first + rest).encode("utf-8")).hexdigest() == digest
    assert db.read_artifact("other-session", digest) is None
    db.close()
