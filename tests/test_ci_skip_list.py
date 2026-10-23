"""No test, and no code a test runs, reads a file on the CI skip list (#750).

A change touching only `CI_SKIP_UNREAD_PATHS` entries skips the test jobs, so a test reading one of them would never
run against a change to it. The scan fails closed: any string literal naming a listed path counts as a read.
"""

import ast
import io
import tokenize
import warnings
from pathlib import Path, PurePosixPath

import yaml

from test_ci_workflow import ROOT, WORKFLOW, _skip_list


SCANNED_DIRS = ("tests", "harness", "harness_modules", "sdk", "scripts", "macrunner", "bakeoff", "tools")

EXEMPT_FILES = {
    # The skip list's own guard: it names every entry to pin the list.
    "tests/test_ci_workflow.py",
    # Passes `LICENSE` and `docs/phase0-results.md` to ci_gate as values and writes them only into a tmp_path repo.
    "tests/test_ci_gate.py",
    # This scan and its synthetic self-test trees.
    "tests/test_ci_skip_list.py",
}

EXEMPT_PAIRS = {
    # Module metadata the settings API returns as a string (`"docs": spec.docs`); never opened.
    ("harness_modules/images/__init__.py", "docs/flux-fast.md"),
}

def _token_types(*names: str) -> set[int]:
    # f-strings tokenize in parts from Python 3.12, t-strings from 3.14.
    return {getattr(tokenize, name) for name in names if hasattr(tokenize, name)}


_STRING_PART_TOKENS = {tokenize.STRING, *_token_types("FSTRING_MIDDLE", "TSTRING_MIDDLE")}
_STRING_STARTS = _token_types("FSTRING_START", "TSTRING_START")
_STRING_ENDS = _token_types("FSTRING_END", "TSTRING_END")


def _docstring_spans(tree: ast.AST, lines: list[str]) -> dict[tuple[int, int], tuple[int, int]]:
    """Map each docstring's start position to its end, in tokenize's (line, character) coordinates."""

    def position(line: int, byte_col: int) -> tuple[int, int]:
        # ast columns count UTF-8 bytes; tokenize columns count characters.
        return line, len(lines[line - 1].encode("utf-8")[:byte_col].decode("utf-8"))

    spans = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        first = node.body[0] if node.body else None
        if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
            # The string's own span, not the statement's, which starts at a `(` around it.
            doc = first.value
            spans[position(doc.lineno, doc.col_offset)] = position(doc.end_lineno, doc.end_col_offset)
    return spans


def _unescape(text: str) -> str:
    """Decode backslash escapes the way a non-raw literal does, so `\\x72` matches `r`."""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            return text.encode("latin-1", "backslashreplace").decode("unicode_escape")
    except UnicodeDecodeError:
        return text


def _literal_text(tok: tokenize.TokenInfo, raw: bool) -> str:
    if tok.type != tokenize.STRING:
        # An f-string part keeps its escapes as written.
        return tok.string if raw else _unescape(tok.string)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", SyntaxWarning)
            value = ast.literal_eval(tok.string)
    except (ValueError, SyntaxError):
        # An f-string tokenized whole (Python < 3.12): drop the prefix and quotes, keep the text.
        body = tok.string.lstrip("rRbBuUfFtT")
        prefix = tok.string[: len(tok.string) - len(body)]
        quote = body[:3] if body[:3] in ('"""', "'''") else body[:1]
        body = body[len(quote) : len(body) - len(quote)]
        return body if "r" in prefix.lower() else _unescape(body)
    # A bytes path opens the file just the same.
    return value.decode("latin-1") if isinstance(value, bytes) else value


def _string_literals(source: str):
    """Yield (line, text) for every string literal and f-string part, skipping docstrings; comments never tokenize as strings."""
    with warnings.catch_warnings():
        # Some scanned sources hold invalid escape sequences; that is for lint to report, not this scan.
        warnings.simplefilter("ignore", SyntaxWarning)
        docstrings = _docstring_spans(ast.parse(source), source.splitlines(keepends=True))
    docstring_end = (0, 0)
    prefixes = []  # Of the f-strings the tokenizer is inside, innermost last.
    for tok in tokenize.generate_tokens(io.StringIO(source).readline):
        if tok.type in _STRING_STARTS:
            prefixes.append(tok.string)
        elif tok.type in _STRING_ENDS:
            prefixes.pop()
        if tok.type not in _STRING_PART_TOKENS:
            continue
        docstring_end = docstrings.get(tok.start, docstring_end)
        if tok.end <= docstring_end:
            continue
        text = _literal_text(tok, raw=bool(prefixes) and "r" in prefixes[-1].lower())
        if isinstance(text, str):
            yield tok.start[0], text


def _names(literal: str, listed: str, basename: str) -> bool:
    if "." not in basename:
        # `LICENSE` counts only as the whole literal, so `LICENSE.txt` and prose don't trip it.
        return literal == basename or ("/" in listed and listed in literal)
    return basename in literal


def find_reads(root: Path, listed_paths: list[str]) -> list[str]:
    """Return `file:line names listed/path` for each scanned literal naming a listed path, minus the exemptions."""
    basenames = {listed: PurePosixPath(listed).name for listed in listed_paths}
    found = []
    for top in SCANNED_DIRS:
        for path in sorted((root / top).rglob("*.py")):
            rel = path.relative_to(root).as_posix()
            if rel in EXEMPT_FILES:
                continue
            source = path.read_text(encoding="utf-8")
            for line, literal in _string_literals(source):
                for listed, basename in basenames.items():
                    if _names(literal, listed, basename) and (rel, listed) not in EXEMPT_PAIRS:
                        found.append(f"{rel}:{line} names {listed}")
    return found


def test_no_scanned_code_reads_a_ci_skip_list_entry():
    listed = _skip_list(yaml.safe_load(WORKFLOW.read_text(encoding="utf-8")))
    assert listed
    found = find_reads(ROOT, listed)
    assert not found, (
        "These files name a CI_SKIP_UNREAD_PATHS entry, so a change to it would skip them. Remove the path from the "
        "list in .github/workflows/ci.yml (and EXPECTED_SKIP_PATHS) in the same change:\n" + "\n".join(found)
    )


def test_scan_reports_a_read_by_path_parts_and_ignores_lookalikes(tmp_path):
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_reads.py").write_text(
        'from pathlib import Path\nROOT = Path(__file__).parents[1]\n\n\n'
        'def test_x():\n    assert (ROOT / "docs" / "phase0-results.md").read_text()\n',
        encoding="utf-8",
    )
    (tests / "test_lookalikes.py").write_text(
        '"""Cites docs/phase0-results.md and LICENSE in a docstring."""\n'
        "# docs/phase0-results.md in a comment\n"
        'NAME = "LICENSE.txt"\nPROSE = "see the LICENSE file"\n',
        encoding="utf-8",
    )

    found = find_reads(tmp_path, ["LICENSE", "docs/phase0-results.md"])

    assert found == ["tests/test_reads.py:6 names docs/phase0-results.md"]


def test_scan_catches_whole_license_and_f_string_parts(tmp_path):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "reader.py").write_text(
        'import pathlib\n\nlicense = pathlib.Path("LICENSE")\nresults = f"{pathlib.Path.cwd()}/docs/phase4-results.md"\n',
        encoding="utf-8",
    )

    found = find_reads(tmp_path, ["LICENSE", "docs/phase4-results.md"])

    assert found == ["scripts/reader.py:3 names LICENSE", "scripts/reader.py:4 names docs/phase4-results.md"]


def test_scan_ignores_only_the_docstring_itself(tmp_path):
    (tmp_path / "tools" / ".venv").mkdir(parents=True)
    (tmp_path / "tools" / "accented.py").write_text(
        '"""' + "é" * 80 + '"""; import pathlib; pathlib.Path("docs/phase0-results.md").read_text()\n',
        encoding="utf-8",
    )
    (tmp_path / "tools" / "parenthesized.py").write_text('("docs/phase0-results.md")\n', encoding="utf-8")
    (tmp_path / "tools" / ".venv" / "reader.py").write_text('open("docs/phase0-results.md")\n', encoding="utf-8")

    found = find_reads(tmp_path, ["docs/phase0-results.md"])

    assert found == ["tools/.venv/reader.py:1 names docs/phase0-results.md", "tools/accented.py:1 names docs/phase0-results.md"]


def test_scan_decodes_bytes_and_f_string_escapes(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_encoded.py").write_text(
        "from pathlib import Path\n"
        'open(b"docs/phase0-results.md").read()\n'
        'Path(f"docs/phase0-\\x72esults.md").read_text()\n'
        'Path(rf"docs\\x2fphase4-results.md")\n',
        encoding="utf-8",
    )

    found = find_reads(tmp_path, ["docs/phase0-results.md", "docs/phase4-results.md"])

    # The raw f-string keeps `\x2f` as written: it still names the basename, though not the full path.
    assert found == [
        "tests/test_encoded.py:2 names docs/phase0-results.md",
        "tests/test_encoded.py:3 names docs/phase0-results.md",
        "tests/test_encoded.py:4 names docs/phase4-results.md",
    ]
