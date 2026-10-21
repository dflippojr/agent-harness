"""Execute the docs workflow's CI wait with synthetic GitHub responses."""

import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest
import yaml

ROOT = Path(__file__).parents[1]


def _bash():
    git = shutil.which("git")
    if git:
        candidate = Path(git).parent.parent / "bin" / "bash.exe"
        if candidate.exists():
            return str(candidate)
    bash = shutil.which("bash")
    if bash:
        return bash
    pytest.skip("Bash is required to exercise the Ubuntu workflow script")


@pytest.mark.parametrize(
    ("run_id", "watch_status", "gate", "expected"),
    [
        ("123", "0", "success", 0),
        ("123", "1", "success", 1),
        ("123", "0", "failure", 1),
        ("123", "0", "skipped", 1),
        ("123", "0", "", 1),
        ("", "0", "success", 1),
    ],
)
def test_ci_wait_requires_run_and_successful_gate(tmp_path, run_id, watch_status, gate, expected):
    workflow = yaml.safe_load((ROOT / ".github/workflows/docs-regen.yml").read_text(encoding="utf-8"))
    step = next(step for step in workflow["jobs"]["regenerate"]["steps"] if step.get("id") == "ci")
    # All network operations and sleeps are replaced; run the actual workflow control flow.
    mock = '''gh() {
      case "$1 $2" in
        "run list") printf '%s' "$MOCK_RUN" ;;
        "run watch") return "$MOCK_WATCH" ;;
        "api "*) printf '%s' "$MOCK_GATE" ;;
        *) return 99 ;;
      esac
    }
    sleep() { :; }
    '''
    script = tmp_path / "wait.sh"
    script.write_text(mock + step["run"], encoding="utf-8", newline='\n')
    result = subprocess.run(
        [_bash(), str(script)], capture_output=True, text=True,
        env={**os.environ, "MOCK_RUN": run_id, "MOCK_WATCH": watch_status, "MOCK_GATE": gate,
             "HEAD_SHA": "synthetic-head", "DOCS_BRANCH": "bot/docs-regen", "GITHUB_REPOSITORY": "owner/repo"},
        timeout=15,
    )
    assert result.returncode == expected, result.stdout + result.stderr
    if not run_id:
        assert "PR CI did not start" in result.stderr
    elif watch_status == "0" and gate != "success":
        assert "Required gate did not pass" in result.stderr


def _guard_step():
    workflow = yaml.safe_load((ROOT / ".github/workflows/docs-regen.yml").read_text(encoding="utf-8"))
    steps = workflow["jobs"]["regenerate"]["steps"]
    return steps, next(step for step in steps if step.get("id") == "guard")


def _runs(*runs):
    return {"total_count": len(runs), "check_runs": [
        {"id": run_id, "name": "gate-guard-target / gate-guard", "status": status, "conclusion": conclusion}
        for run_id, status, conclusion in runs]}


def test_merge_waits_for_the_trusted_gate_guard_on_the_head_sha():
    steps, guard = _guard_step()
    ids = [step.get("id") or step.get("name") for step in steps]
    assert ids.index("ci") < ids.index("guard") < ids.index("merge-token") < ids.index("Merge the docs PR")
    assert guard["env"]["HEAD_SHA"] == "${{ steps.regen.outputs.sha }}"
    assert guard["env"]["GUARD_CHECK"] == "gate-guard-target / gate-guard"
    assert "checks" in yaml.safe_load((ROOT / ".github/workflows/docs-regen.yml").read_text(encoding="utf-8"))["permissions"]


@pytest.mark.parametrize(
    ("responses", "expected", "message"),
    [
        ([_runs((9, "completed", "success"))], 0, ""),
        ([_runs(), _runs((9, "queued", None)), _runs((9, "in_progress", None)), _runs((9, "completed", "success"))], 0, ""),
        # A superseded run of the same head is cancelled; the newest run decides.
        ([_runs((7, "completed", "cancelled"), (9, "completed", "success"))], 0, ""),
        ([_runs((7, "completed", "success"), (9, "completed", "failure"))], 1, "conclusion: failure"),
        ([_runs((9, "completed", "cancelled"))], 1, "conclusion: cancelled"),
        ([_runs((9, "completed", "skipped"))], 1, "conclusion: skipped"),
        ([_runs()], 1, "never appeared"),
        ([_runs((9, "queued", None))], 1, "still queued"),
        ([_runs((9, "in_progress", None))], 1, "still in_progress"),
    ],
)
def test_guard_wait_gates_the_merge(tmp_path, responses, expected, message):
    jq = shutil.which("jq")
    if not jq:
        pytest.skip("jq is required to run the workflow's check-runs filter")
    _, step = _guard_step()
    responses_dir = tmp_path / "responses"
    responses_dir.mkdir()
    for index, response in enumerate(responses):
        (responses_dir / f"{index}.json").write_text(json.dumps(response), encoding="utf-8", newline="\n")
    # The check-runs API is replaced by recorded JSON, run through the workflow's own jq filter;
    # the last recorded response repeats. sleep only counts, so the bounded loop stays fast.
    mock = '''gh() {
      [ "$1" = api ] || return 99
      printf '%s\n' "$*" >> "$MOCK_DIR/calls"
      polls="$(cat "$MOCK_DIR/polls" 2>/dev/null || echo 0)"
      echo $((polls + 1)) > "$MOCK_DIR/polls"
      last=$(($(ls "$MOCK_DIR/responses" | wc -l) - 1))
      [ "$polls" -le "$last" ] || polls="$last"
      filter=""
      while [ $# -gt 0 ]; do
        if [ "$1" = --jq ]; then filter="$2"; fi
        shift
      done
      "$JQ" -r "$filter" "$MOCK_DIR/responses/$polls.json"
    }
    sleep() { echo "$1" >> "$MOCK_DIR/sleeps"; }
    '''
    script = tmp_path / "guard.sh"
    script.write_text(mock + step["run"], encoding="utf-8", newline="\n")
    result = subprocess.run(
        [_bash(), str(script)], capture_output=True, text=True,
        env={**os.environ, "MOCK_DIR": str(tmp_path), "JQ": jq, "HEAD_SHA": "synthetic-head",
             "GUARD_CHECK": step["env"]["GUARD_CHECK"], "GITHUB_REPOSITORY": "owner/repo"},
        timeout=30,
    )
    assert result.returncode == expected, result.stdout + result.stderr
    if message:
        assert message in result.stderr
    calls = (tmp_path / "calls").read_text(encoding="utf-8")
    assert "commits/synthetic-head/check-runs" in calls
    assert "check_name=gate-guard-target / gate-guard" in calls
    polls = int((tmp_path / "polls").read_text(encoding="utf-8"))
    if expected == 1 and "conclusion" not in message:
        assert polls == 60  # bounded: 60 polls, 30 seconds apart
