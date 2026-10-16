"""Run pyright (pyrightconfig.json) and fail only on errors the committed baseline does not record (#729).

    python tools/lint/pyright_baseline.py            # CI's `lint` job: fail on any error not in the baseline
    python tools/lint/pyright_baseline.py --write    # rewrite tools/lint/pyright-baseline.json from this run

An error is keyed by file, rule and message, not line, so edits elsewhere in a file do not disturb the baseline. The
baseline holds how many errors each key had; a run fails when a key has more. Fixed errors only print a notice: run
--write to shrink the baseline so they cannot come back. Pyright analyses the imports of the interpreter running
this script, so install requirements.txt, requirements-repomap.txt and requirements-telemetry.txt into it first.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Callable, NamedTuple

ROOT = Path(__file__).resolve().parents[2]
BASELINE = Path(__file__).resolve().with_name("pyright-baseline.json")

Runner = Callable[[list[str]], subprocess.CompletedProcess]


class Error(NamedTuple):
    file: str
    line: int
    column: int
    rule: str
    message: str

    @property
    def key(self) -> tuple[str, str, str]:
        return self.file, self.rule, self.message


def _run(command: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(command, cwd=ROOT, capture_output=True, text=True, encoding="utf-8")


def pyright_report(run: Runner | None = None) -> dict:
    """Pyright's --outputjson report. Exit 1 only means errors were found; anything else is a failed run."""
    command = [sys.executable, "-m", "pyright", "--outputjson", "--pythonpath", sys.executable,
               "-p", str(ROOT / "pyrightconfig.json")]
    result = (run or _run)(command)
    if result.returncode not in (0, 1):
        raise RuntimeError(f"pyright exited {result.returncode}: {(result.stderr or result.stdout).strip()[:2000]}")
    report = json.loads(result.stdout)
    if not report.get("summary", {}).get("filesAnalyzed"):
        raise RuntimeError("pyright analysed no files; check the include list in pyrightconfig.json")
    return report


def errors(report: dict, root: Path = ROOT) -> list[Error]:
    found = []
    for diagnostic in report.get("generalDiagnostics", []):
        if diagnostic.get("severity") != "error":
            continue
        path = Path(diagnostic["file"])
        file = path.relative_to(root).as_posix() if path.is_relative_to(root) else path.as_posix()
        start = diagnostic.get("range", {}).get("start", {})
        found.append(Error(file, start.get("line", 0) + 1, start.get("character", 0) + 1,
                           diagnostic.get("rule", ""), diagnostic["message"].replace(str(root), "<root>")))
    return found


def counts(found: list[Error]) -> Counter:
    return Counter(error.key for error in found)


def baseline_document(found: list[Error]) -> str:
    rows = [{"file": file, "rule": rule, "message": message, "count": count}
            for (file, rule, message), count in sorted(counts(found).items())]
    return json.dumps(rows, indent=2, ensure_ascii=False) + "\n"


def read_baseline(text: str) -> Counter:
    return Counter({(row["file"], row["rule"], row["message"]): row["count"] for row in json.loads(text)})


def compare(found: list[Error], baseline: Counter) -> tuple[list[Error], int]:
    """The errors of every key that has more than the baseline allows, and how many baseline errors are gone."""
    current = counts(found)
    new = [error for error in found if current[error.key] > baseline.get(error.key, 0)]
    fixed = sum(max(allowed - current.get(key, 0), 0) for key, allowed in baseline.items())
    return new, fixed


def _annotation(error: Error) -> str:
    message = error.message.replace("%", "%25").replace("\r", "").replace("\n", "%0A")
    return f"::error file={error.file},line={error.line},col={error.column},title=pyright {error.rule}::{message}"


def main(argv: list[str] | None = None, run: Runner | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    found = errors(pyright_report(run))
    if "--write" in argv:
        BASELINE.write_text(baseline_document(found), encoding="utf-8", newline="\n")
        print(f"recorded {len(found)} pyright errors in {BASELINE}")
        return 0
    new, fixed = compare(found, read_baseline(BASELINE.read_text(encoding="utf-8")))
    github = bool(os.environ.get("GITHUB_ACTIONS"))
    for error in new:
        print(_annotation(error) if github else
              f"{error.file}:{error.line}:{error.column}: {error.rule}: {error.message}")
    keys = {error.key for error in new}
    if keys:
        print(f"pyright: {len(keys)} file/rule/message combination(s) have more errors than the baseline records "
              f"(listed above with every error of that kind in the file). Fix them; do not grow the baseline.",
              file=sys.stderr)
    if fixed:
        print(f"pyright: {fixed} baseline error(s) are fixed; run python tools/lint/pyright_baseline.py --write "
              f"to shrink the baseline.")
    print(f"pyright: {len(found)} errors, {len(found) - len(new)} recorded in the baseline")
    return 1 if keys else 0


if __name__ == "__main__":
    sys.exit(main())
