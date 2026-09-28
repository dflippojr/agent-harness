import hashlib
import shutil

from harness import compaction
from harness.config import _mask_min_chars
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
    diagnostic = _turn(content="Error: successful diagnostic output" + "x" * 2000)
    masked, artifacts, _ = compaction.mask_used_results(diagnostic, {"c1": {"ok": True}}, 2000)
    assert masked[1]["content"].startswith("[Observation receipt]")
    assert artifacts


def test_mask_threshold_config_fallback():
    assert _mask_min_chars(None) == 2000
    assert _mask_min_chars("invalid") == 2000
    assert _mask_min_chars(0) == 2000
    assert _mask_min_chars(3500) == 3500


def test_mask_receipt_uses_full_output_when_context_result_was_capped():
    full = "z" * 25000
    messages = _turn(content=full[:2000])
    masked, artifacts, _ = compaction.mask_used_results(
        messages, {"c1": {"ok": True}}, 2000, {"c1": full})
    digest = hashlib.sha256(full.encode("utf-8")).hexdigest()
    assert f"characters={len(full)}" in masked[1]["content"]
    assert digest in artifacts and artifacts[digest] == full


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
    db.delete_session("session-a")
    assert db.read_artifact("session-a", digest) is None
    db.close()


def test_mask_threshold_bounds_and_odd_types():
    assert _mask_min_chars(1) == 1
    assert _mask_min_chars(10_000_000) == 10_000_000
    assert _mask_min_chars(10_000_001) == 2000
    assert _mask_min_chars(-5) == 2000
    assert _mask_min_chars(True) == 2000
    assert _mask_min_chars(float("inf")) == 2000
    assert _mask_min_chars("4096") == 4096


def test_mask_threshold_loaded_from_yaml(tmp_path):
    from harness import config
    cfg_dir = tmp_path / "cfg"
    shutil.copytree(config.ROOT / "config", cfg_dir)
    text = (cfg_dir / "harness.yaml").read_text(encoding="utf-8")
    assert "mask_min_chars: 2000" in text
    assert config.load(cfg_dir, tmp_path / "data").mask_min_chars == 2000
    for value, expected in ((5000, 5000), (0, 2000), ("many", 2000)):
        (cfg_dir / "harness.yaml").write_text(
            text.replace("mask_min_chars: 2000", f"mask_min_chars: {value}"), encoding="utf-8")
        assert config.load(cfg_dir, tmp_path / "data").mask_min_chars == expected


def test_receipts_are_not_masked_again():
    once, artifacts, saved = compaction.mask_used_results(_turn(), {"c1": {"ok": True}}, 2000)
    assert artifacts and saved > 0
    twice, artifacts, saved = compaction.mask_used_results(once, {"c1": {"ok": True}}, 2000)
    assert twice == once
    assert not artifacts and saved == 0
    # Even with a tiny threshold a receipt is never wrapped in a second receipt.
    again, artifacts, _ = compaction.mask_used_results(once, {"c1": {"ok": True}}, 1)
    assert again == once and not artifacts


def test_artifact_cap_scoping_and_ranges(tmp_path):
    db = Database(tmp_path / "harness.sqlite3")
    text = "abcdefghij" * 3000
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    db.put_artifact("s1", digest, text)
    db.put_artifact("s1", digest, "ignored duplicate")
    db.put_artifact("s2", digest, "other session")

    assert db.full_artifact("s1", digest) == text  # the daemon-side read is not capped
    assert db.full_artifact("missing", digest) is None
    head, truncated = db.read_artifact("s1", digest)
    assert head == text[:20000] and truncated
    assert db.read_artifact("s1", digest, 5, 15) == (text[5:15], False)
    capped, truncated = db.read_artifact("s1", digest, 0, 29999)
    assert len(capped) == 20000 and truncated
    assert db.read_artifact("s1", digest, 29990, 40000) == (text[29990:], False)
    assert db.read_artifact("s2", digest) == ("other session", False)
    assert db.read_artifact("s3", digest) is None
    db.delete_session("s1")
    assert db.read_artifact("s1", digest) is None
    assert db.read_artifact("s2", digest) is not None
    db.close()


def test_artifact_with_embedded_nuls_round_trips(tmp_path):
    db = Database(tmp_path / "nul.db")
    text = "\0start" + "a" * 100 + "\0mid" + "b" * 30000 + "end\0"
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    db.put_artifact("s1", digest, text)
    assert db.full_artifact("s1", digest) == text
    got, truncated = db.read_artifact("s1", digest, 0, 50)
    assert (got, truncated) == (text[:50], False)
    assert db.read_artifact("s1", digest, 103, 110) == (text[103:110], False)  # spans the middle NUL
    assert db.read_artifact("s1", digest, len(text) - 6) == (text[-6:], False)  # ends in NUL
    chunks, pos = [], 0
    while True:
        chunk, more = db.read_artifact("s1", digest, pos)
        chunks.append(chunk)
        pos += len(chunk)
        if not more:
            break
    assert len(chunks[0]) == 20000 and len(chunks) == 2
    recovered = "".join(chunks)
    assert recovered == text
    assert hashlib.sha256(recovered.encode("utf-8")).hexdigest() == digest
    db.delete_session("s1")
    assert db.read_artifact("s1", digest) is None
