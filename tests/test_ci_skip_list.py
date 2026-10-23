"""No test, and no code a test runs, reads a file on the CI skip list (#750).

A change touching only `CI_SKIP_UNREAD_PATHS` entries skips the test jobs, so a test reading one of them would never
run against a change to it. The scan fails closed: any string literal naming a listed path counts as a read, and so
does a literal naming a listed path's directory in a file that lists directories.
"""

import ast
import io
import re
import tokenize
import warnings
from pathlib import Path, PurePosixPath

import pytest
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

# (file, listed path) pairs whose literal names the path but nothing opens it, each with its reason.
EXEMPT_PAIRS: set[tuple[str, str]] = set()

# A file that lists directories (`git ls-files`, `glob`, `os.walk`, ...) is taken to read whatever lies in each one
# it names.
LISTS = re.compile(r"ls-files|\b(?:r?glob|iterdir|walk|listdir|scandir)\b")


def _history_skips(source: str, listed: str) -> bool:
    """Whether the `history = re.compile(...)` pattern in `source` matches `listed`."""
    for node in ast.walk(ast.parse(source)):
        if (
            isinstance(node, ast.Assign)
            and [getattr(target, "id", None) for target in node.targets] == ["history"]
            and isinstance(node.value, ast.Call)
            and node.value.args
            and isinstance(node.value.args[0], ast.Constant)
        ):
            return bool(re.search(node.value.args[0].value, listed))
    return False


def _skips_all(source: str, listed: str) -> bool:
    return True


def _listings(source: str) -> list[tuple[str, str]]:
    """Each listing word in `source`, with the stripped line it is on."""
    found = []
    for match in LISTS.finditer(source):
        start = source.rfind("\n", 0, match.start()) + 1
        end = source.find("\n", match.end())
        found.append((match.group(), source[start : end if end >= 0 else len(source)].strip()))
    return found


# (file, directory) -> (every listing the file has today, as `_listings` returns them; whether that file's listing of
# the directory skips a listed path, given the file's source). Any other listing in the file voids the exemption.
EXEMPT_LISTINGS = {
    # Lists docs/ with git ls-files and skips history docs by its `history` regex; any other listed doc it reads counts.
    ("tests/test_product_naming.py", "docs"): (
        [("ls-files", 'tracked = subprocess.run(["git", "ls-files", "harness", "harness_modules", "sdk", "config", "ops", "install",')],
        _history_skips,
    ),
    # Globs only docs/fragments/*.yaml; names docs alone as that folder's parent and in tmp_path trees.
    ("tests/test_docs_build.py", "docs"): (
        [("glob", 'assert len(list((ROOT / "docs" / "fragments").glob("*.yaml"))) >= 7')],
        _skips_all,
    ),
    # Reads docs/CI-CD.md by name; `src/docs` is a citation fixture. Its rglob and iterdir walk tmp_path workspaces.
    ("tests/test_review_runner.py", "docs"): (
        [
            ("rglob", 'assert sorted(path.relative_to(workspace).as_posix() for path in workspace.rglob("*")) == ['),
            ("iterdir", '"entries": sorted(entry.name for entry in cwd.iterdir()), "env": dict(os.environ),'),
        ],
        _skips_all,
    ),
    # `^/opt/docs/` is a policy rule matched against a made-up path; the file lists nothing, a comment says "glob" and "walk".
    ("tests/test_claude_reads.py", "docs"): (
        [
            ("glob", "# Read, Grep and LS paths are literal: glob characters in a folder name don't end the walk."),
            ("walk", "# Read, Grep and LS paths are literal: glob characters in a folder name don't end the walk."),
        ],
        _skips_all,
    ),
}


def _listing_skips(rel: str, directory: str | None, source: str, listed: str) -> bool:
    exemption = EXEMPT_LISTINGS.get((rel, directory))
    if not exemption:
        return False
    listings, skips = exemption
    return _listings(source) == listings and skips(source, listed)


def _token_types(*names: str) -> set[int]:
    # f-strings tokenize in parts from Python 3.12, t-strings from 3.14.
    return {getattr(tokenize, name) for name in names if hasattr(tokenize, name)}


_STRING_PARTS = _token_types("FSTRING_MIDDLE", "TSTRING_MIDDLE")
_STRING_STARTS = _token_types("FSTRING_START", "TSTRING_START")
_STRING_ENDS = _token_types("FSTRING_END", "TSTRING_END")


def _docstrings(tree: ast.AST) -> list[ast.Constant]:
    docs = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        first = node.body[0] if node.body else None
        if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
            # The string's own node, not the statement's, which starts at a `(` around it.
            docs.append(first.value)
    return docs


def _docstring_spans(docs: list[ast.Constant], lines: list[str]) -> dict[tuple[int, int], tuple[int, int]]:
    """Map each docstring's start position to its end, in tokenize's (line, character) coordinates."""

    def position(line: int, byte_col: int) -> tuple[int, int]:
        # ast columns count UTF-8 bytes; tokenize columns count characters.
        return line, len(lines[line - 1].encode("utf-8")[:byte_col].decode("utf-8"))

    return {position(doc.lineno, doc.col_offset): position(doc.end_lineno, doc.end_col_offset) for doc in docs}


def _unescape(text: str) -> str:
    """Decode backslash escapes the way a non-raw literal does, so `\\x72` matches `r`."""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            return text.encode("latin-1", "backslashreplace").decode("unicode_escape")
    except UnicodeDecodeError:
        return text


def _literal_text(tok: tokenize.TokenInfo) -> str:
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


def _segments(node: ast.expr) -> list[str | None]:
    """The literal text of an operand of `+`, with None wherever a value is computed."""
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _segments(node.left) + _segments(node.right)
    if isinstance(node, ast.Constant) and isinstance(node.value, (str, bytes)):
        return [node.value.decode("latin-1") if isinstance(node.value, bytes) else node.value]
    if isinstance(node, ast.JoinedStr):
        return [part.value if isinstance(part, ast.Constant) else None for part in node.values]
    return [None]


def _chain(node: ast.expr):
    """The `+` nodes `_segments` recurses through from `node`; a `+` inside a call is a chain of its own."""
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        yield node
        yield from _chain(node.left)
        yield from _chain(node.right)


def _joined_literals(tree: ast.AST, docs: list[ast.Constant]):
    """Yield (line, text) for adjacent literals the tokenizer sees apart: `"a" "b"` and each literal run of `"a" + "b"`."""
    docstrings = {id(doc) for doc in docs}
    inner = set()  # `+` nodes inside a chain already joined.
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, (str, bytes)) and id(node) not in docstrings:
            yield node.lineno, _segments(node)[0]
        elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add) and id(node) not in inner:
            inner.update(id(child) for child in _chain(node))
            run = []
            for segment in [*_segments(node), None]:
                if segment is not None:
                    run.append(segment)
                elif run:
                    yield node.lineno, "".join(run)
                    run = []


def _string_literals(source: str):
    """Yield (line, text) for every string literal and f-string part, skipping docstrings; comments never tokenize as strings."""
    with warnings.catch_warnings():
        # Some scanned sources hold invalid escape sequences; that is for lint to report, not this scan.
        warnings.simplefilter("ignore", SyntaxWarning)
        tree = ast.parse(source)
    docs = _docstrings(tree)
    yield from _joined_literals(tree, docs)
    # Physical lines, as tokenize counts them; str.splitlines also splits at U+2028 and friends.
    docstrings = _docstring_spans(docs, io.StringIO(source).readlines())
    docstring_end = (0, 0)
    prefixes = []  # Of the f-strings the tokenizer is inside, innermost last.
    parts = []  # Consecutive f-string parts: the tokenizer splits one run of text at `{{` and `\N{...}`.
    for tok in tokenize.generate_tokens(io.StringIO(source).readline):
        if tok.type in _STRING_PARTS:
            parts.append(tok)
            continue
        if parts:
            text = "".join(part.string for part in parts)
            # An f-string part keeps its escapes as written.
            yield parts[0].start[0], text if "r" in prefixes[-1].lower() else _unescape(text)
            parts = []
        if tok.type in _STRING_STARTS:
            prefixes.append(tok.string)
        elif tok.type in _STRING_ENDS:
            prefixes.pop()
        if tok.type != tokenize.STRING:
            continue
        docstring_end = docstrings.get(tok.start, docstring_end)
        if tok.end <= docstring_end:
            continue
        text = _literal_text(tok)
        if isinstance(text, str):
            yield tok.start[0], text


# Where a path component starts: the literal's start, a path separator, a space in a shell command, or a git `rev:`.
_START = r"(?:^|[\s/\\:])"


def _names(literal: str, listed: str, basename: str) -> bool:
    if "." not in basename:
        # `LICENSE` counts only as the last path component, so `LICENSE.txt` and prose don't trip it. A space or `:`
        # before it is a shell word or a git revision, as in `git show HEAD:LICENSE`.
        return bool(re.search(rf"{_START}{re.escape(basename)}$", literal)) or ("/" in listed and listed in literal)
    return basename in literal


def _lists(literal: str, directory: str) -> bool:
    """Whether `literal` names `directory` as its last path component, or a glob directly inside it, as in
    `git ls-files -- docs/*.md`."""
    return bool(re.search(rf"{_START}{re.escape(directory)}(?:[/\\][^/\\]*[*?[].*)?[/\\]?$", literal))


def find_reads(root: Path, listed_paths: list[str]) -> list[str]:
    """Return `file:line names listed/path` for each scanned literal naming a listed path, and `file:line,line lists
    dir, which holds listed/path, ...` for a file that lists directories and names a listed path's directory, minus the
    exemptions."""
    basenames = {listed: PurePosixPath(listed).name for listed in listed_paths}
    # The repository root holds LICENSE, but no literal names it; a listing of the whole tree is not caught.
    directories = {listed: str(PurePosixPath(listed).parent) for listed in listed_paths if "/" in listed}
    found = []
    for top in SCANNED_DIRS:
        for path in sorted((root / top).rglob("*.py")):
            rel = path.relative_to(root).as_posix()
            if rel in EXEMPT_FILES:
                continue
            source = path.read_text(encoding="utf-8")
            lists = bool(LISTS.search(source))
            reads = set()
            listings = {}  # Directory -> (lines naming it, listed paths it holds that no exemption skips).
            for line, literal in _string_literals(source):
                for index, (listed, basename) in enumerate(basenames.items()):
                    if _names(literal, listed, basename) and (rel, listed) not in EXEMPT_PAIRS:
                        reads.add((line, index, f"{rel}:{line} names {listed}"))
                    directory = directories.get(listed)
                    if lists and directory and _lists(literal, directory) and not _listing_skips(rel, directory, source, listed):
                        lines, held = listings.setdefault(directory, (set(), {}))
                        lines.add(line)
                        held[listed] = index
            for directory, (lines, held) in listings.items():
                paths = ", ".join(sorted(held, key=held.get))
                at = ",".join(str(line) for line in sorted(lines))
                reads.add((min(lines), min(held.values()), f"{rel}:{at} lists {directory}, which holds {paths}"))
            found.extend(read for _, _, read in sorted(reads))
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


def test_scan_joins_split_f_string_parts_and_counts_physical_lines(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_split.py").write_text(
        "from pathlib import Path\n"
        'Path(f"docs/phase0-\\N{LATIN SMALL LETTER R}esults.md").read_text()\n',
        encoding="utf-8",
    )
    # A real U+2028 inside a docstring: one physical line to tokenize, two to str.splitlines.
    (tmp_path / "tests" / "test_separator.py").write_text(
        '"""Before\u2028cites docs/phase0-results.md."""\n', encoding="utf-8"
    )

    found = find_reads(tmp_path, ["docs/phase0-results.md"])

    assert found == ["tests/test_split.py:2 names docs/phase0-results.md"]


def test_scan_reports_a_listing_of_a_listed_paths_directory(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_lists.py").write_text(
        "import os\nimport subprocess\nfrom pathlib import Path\nROOT = Path(__file__).parents[1]\n"
        'tracked = subprocess.run(["git", "ls-files", "docs"], capture_output=True).stdout\n'
        'walked = os.walk(f"{ROOT}/docs")\n'
        'globbed = ROOT.glob("./docs/*.md")\n'
        'licenses = (ROOT / "third_party").iterdir()\n',
        encoding="utf-8",
    )
    # Names the directory but lists nothing; lists, but names `docs` only in a comment or as part of a longer name.
    (tmp_path / "tests" / "test_names_docs.py").write_text(
        'from pathlib import Path\nguide = (Path("docs") / "web.md").read_text()\n', encoding="utf-8"
    )
    (tmp_path / "tests" / "test_lookalikes.py").write_text(
        "import os\n# os.listdir(\"docs\")\nsites = os.listdir(\"docs-site\")\nmine = os.listdir(\"mydocs\")\n",
        encoding="utf-8",
    )

    found = find_reads(tmp_path, ["LICENSE", "third_party/Real-ESRGAN.LICENSE", "docs/phase0-results.md"])

    assert found == [
        "tests/test_lists.py:5,6,7 lists docs, which holds docs/phase0-results.md",
        "tests/test_lists.py:8 lists third_party, which holds third_party/Real-ESRGAN.LICENSE",
    ]


def test_product_naming_lists_docs_but_skips_every_listed_doc(tmp_path):
    # Its exemption holds only for the listed docs its `history` regex skips; any other one it lists counts.
    rel = "tests/test_product_naming.py"
    (tmp_path / "tests").mkdir()
    (tmp_path / rel).write_text((ROOT / rel).read_text(encoding="utf-8"), encoding="utf-8")

    found = find_reads(tmp_path, ["docs/phase0-results.md", "docs/flux-fast.md"])

    assert len(found) == 1
    assert found[0].startswith(f"{rel}:")
    assert found[0].endswith(" lists docs, which holds docs/flux-fast.md")


def test_scan_matches_an_extensionless_name_as_the_last_path_component(tmp_path):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "license.py").write_text(
        "import os\nfrom pathlib import Path\nROOT = Path(__file__).parents[1]\n"
        'a = open(f"{ROOT}/LICENSE")\n'
        'b = open("./LICENSE")\n'
        'c = open("..\\\\LICENSE")\n'
        'd = open(os.getcwd() + "/LICENSE")\n'
        'e = "LICENSE.txt"\n'
        'f = "Real-ESRGAN.LICENSE"\n'
        'g = "see the LICENSE file"\n',
        encoding="utf-8",
    )

    found = find_reads(tmp_path, ["LICENSE", "third_party/Real-ESRGAN.LICENSE"])

    assert found == [
        "scripts/license.py:4 names LICENSE",
        "scripts/license.py:5 names LICENSE",
        "scripts/license.py:6 names LICENSE",
        "scripts/license.py:7 names LICENSE",
        "scripts/license.py:9 names third_party/Real-ESRGAN.LICENSE",
    ]


def test_scan_joins_concatenated_literals(tmp_path):
    (tmp_path / "harness").mkdir()
    (tmp_path / "harness" / "joined.py").write_text(
        "from pathlib import Path\nROOT = Path(__file__).parents[1]\n"
        'a = open("docs/phase0-" "results.md")\n'
        'b = open(\n    "docs/phase0-"\n    "results.md"\n)\n'
        'c = open("docs/phase0-" + "results.md")\n'
        'd = open(str(ROOT) + "/docs/phase0-" + f"results.md")\n'
        'e = open("docs/phase0-results.md")\n',
        encoding="utf-8",
    )

    found = find_reads(tmp_path, ["docs/phase0-results.md"])

    # One report per literal, however many ways it is matched.
    assert found == [f"harness/joined.py:{line} names docs/phase0-results.md" for line in (3, 5, 8, 9, 10)]


def test_scan_joins_a_chain_nested_in_a_call_and_reads_shell_strings(tmp_path):
    (tmp_path / "harness").mkdir()
    (tmp_path / "harness" / "nested.py").write_text(
        "from pathlib import Path\nROOT = Path(__file__).parents[1]\n"
        'a = str(ROOT) + Path("docs/phase0-" + "results.md").read_text()\n',
        encoding="utf-8",
    )
    (tmp_path / "harness" / "shell.py").write_text(
        "import subprocess\n"
        'subprocess.run("git ls-files docs", shell=True)\n'
        'subprocess.run("git ls-files -- docs/*.md".split())\n'
        'subprocess.run(["git", "show", "HEAD:LICENSE"])\n',
        encoding="utf-8",
    )

    found = find_reads(tmp_path, ["LICENSE", "docs/phase0-results.md"])

    assert found == [
        "harness/nested.py:3 names docs/phase0-results.md",
        "harness/shell.py:2,3 lists docs, which holds docs/phase0-results.md",
        "harness/shell.py:4 names LICENSE",
    ]


# A docs/ listing that each listing exemption's file does not have today; the first three are the review's probes.
NEW_DOCS_LISTINGS = {
    "tests/test_product_naming.py": '[p.read_text() for p in (ROOT / "docs").glob("*.md")]',
    "tests/test_docs_build.py": '[p.read_text() for p in (ROOT / "docs").glob("*-results.md")]',
    "tests/test_review_runner.py": 'for p in (ROOT / "docs").glob("*.md"): p.read_text()',
    "tests/test_claude_reads.py": '[p.read_text() for p in Path("docs").iterdir()]',
}


@pytest.mark.parametrize("rel", sorted(NEW_DOCS_LISTINGS))
def test_a_new_listing_voids_a_listing_exemption(tmp_path, rel):
    # Each exemption holds for the file as it is today and for no listing added to it.
    assert sorted(file for file, _ in EXEMPT_LISTINGS) == sorted(NEW_DOCS_LISTINGS)
    source = (ROOT / rel).read_text(encoding="utf-8")
    copy = tmp_path / rel
    copy.parent.mkdir(parents=True)
    copy.write_text(source, encoding="utf-8")
    assert find_reads(tmp_path, ["docs/phase0-results.md"]) == []

    copy.write_text(source + NEW_DOCS_LISTINGS[rel] + "\n", encoding="utf-8")
    found = find_reads(tmp_path, ["docs/phase0-results.md"])

    assert len(found) == 1
    assert found[0].startswith(f"{rel}:")
    assert found[0].endswith(f",{source.count(chr(10)) + 1} lists docs, which holds docs/phase0-results.md")


def test_a_changed_listing_voids_a_listing_exemption(tmp_path):
    rel = "tests/test_docs_build.py"
    source = (ROOT / rel).read_text(encoding="utf-8")
    assert '(ROOT / "docs" / "fragments").glob("*.yaml")' in source
    (tmp_path / "tests").mkdir()
    (tmp_path / rel).write_text(source.replace('(ROOT / "docs" / "fragments").glob', '(ROOT / "docs").glob'), encoding="utf-8")

    found = find_reads(tmp_path, ["docs/phase0-results.md"])

    assert len(found) == 1
    assert found[0].startswith(f"{rel}:")
    assert found[0].endswith(" lists docs, which holds docs/phase0-results.md")
