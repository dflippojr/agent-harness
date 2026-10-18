"""Execute the docs workflow's CI wait with synthetic GitHub responses."""

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
