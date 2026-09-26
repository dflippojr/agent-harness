"""Line comments on the Changes diff: parsing, staleness, and the draft -> one follow-up flow."""

from __future__ import annotations

import subprocess

from harness import review_comments as rc
from harness.llm import Completion

from test_api import make_client, wait_for
from test_daemon import call

DIFF = """diff --git a/a.txt b/a.txt
index 111..222 100644
--- a/a.txt
+++ b/a.txt
@@ -1,4 +1,4 @@
 one
-two
-three
+TWO
+THREE
 four
diff --git a/gone.txt b/gone.txt
deleted file mode 100644
--- a/gone.txt
+++ /dev/null
@@ -1,2 +0,0 @@
-bye
-now
"""


def test_parse_diff_sides_and_ranges():
    files = rc.parse_diff(DIFF)
    assert [f["name"] for f in files] == ["a.txt", "gone.txt"]
    assert rc.side_lines(files, "a.txt", "old") == {1: "one", 2: "two", 3: "three", 4: "four"}
    assert rc.side_lines(files, "a.txt", "new") == {1: "one", 2: "TWO", 3: "THREE", 4: "four"}
    assert rc.side_lines(files, "gone.txt", "old") == {1: "bye", 2: "now"}
    assert rc.side_lines(files, "gone.txt", "new") == {}


def _comment(**kw):
    base = {"repo": ".", "path": "gone.txt", "side": "old", "start_line": 1, "end_line": 2,
            "quoted": ["bye", "now"], "comment": "why delete?", "base": "b", "head": "h"}
    return rc.validate({**base, **kw})


def test_validate_rejects_bad_ranges():
    for bad in ({"side": "middle"}, {"start_line": 3, "end_line": 2}, {"quoted": ["x"]}, {"comment": " "}):
        try:
            _comment(**bad)
        except ValueError:
            continue
        raise AssertionError(bad)


def test_deleted_range_round_trips_and_stale_is_flagged():
    repos = [{"path": ".", "diff": DIFF}]
    fresh = _comment()
    assert rc.current_quote(fresh, repos) == (False, ["bye", "now"])
    msg = rc.format_message([fresh], repos)
    assert "gone.txt, line 1-2 in the removed (old) lines" in msg
    assert "> bye" in msg
    assert "STALE" not in msg
    stale = _comment(path="a.txt", side="new", start_line=2, end_line=3, quoted=["two", "three"])
    is_stale, now = rc.current_quote(stale, repos)
    assert is_stale
    assert now == ["TWO", "THREE"]
    msg = rc.format_message([stale], repos)
    assert "STALE" in msg
    assert "> TWO" in msg
    assert "> two" not in msg
    assert rc.current_quote(_comment(path="missing.txt"), repos)[0]


def test_draft_survives_restart_and_sends_one_followup(tmp_path):
    steps = [Completion(tool_calls=[call("write_file", 0, path="repo/new.txt", content="hello\nworld\n")]),
             Completion(content="wrote it"), Completion(content="fixed")]
    client, m, _ = make_client(tmp_path, steps)
    with client:
        s = client.post("/sessions", json={"prompt": "make a file"}).json()
        repo = m.cfg.workspaces_dir / s["id"] / "repo"
        repo.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        wait_for(lambda: m.db.get_session(s["id"])["status"] == "done")
        parsed = client.get(f"/sessions/{s['id']}/changes").json()["repos"][0]["parsed"]
        assert rc.side_lines(parsed, "new.txt", "new") == {1: "hello", 2: "world"}
        url = f"/sessions/{s['id']}/review-comments"
        made = client.post(url, json={"repo": "repo", "path": "new.txt", "side": "new", "start_line": 1,
                                      "end_line": 2, "quoted": ["hello", "world"], "comment": "greet better"})
        assert made.status_code == 201
        assert client.post(url, json={"path": "x", "side": "new", "start_line": 1, "quoted": [], "comment": "c"}
                           ).status_code == 400
        assert [c["comment"] for c in client.get(url).json()] == ["greet better"]
        # a fresh Database handle on the same file (daemon restart) still sees the draft
        from harness.db import Database
        assert Database(m.cfg.db_path).list_review_comments(s["id"])
        sent = client.post(url + "/send")
        assert sent.status_code == 200
        wait_for(lambda: m.db.get_session(s["id"])["status"] == "done")
        msgs = [e["data"]["content"] for e in m.db.events(s["id"]) if e["type"] == "user_message"]
        assert len(msgs) == 2
        assert "new.txt, line 1-2 in the new lines" in msgs[1]
        assert "greet better" in msgs[1]
        assert client.get(url).json() == []
        assert client.post(url + "/send").status_code == 400
        assert client.delete(f"{url}/rc-nope").status_code == 404
