import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "docs"))
import build  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
BEGIN, END = "<!-- generated:begin readme-docs-index -->", "<!-- generated:end readme-docs-index -->"
COMP = "<!-- generated:begin readme-components -->\nold\n<!-- generated:end readme-components -->"
README = f"intro text\n\n## Documentation\n\n{BEGIN}\nold\n{END}\n\n{COMP}\n\nafter text\n"
MODULES = "# Modules\n\n<!-- generated:begin modules-list -->\nold\n<!-- generated:end modules-list -->\n"


def frag(title="Topic", order=10, target="readme-docs-index", **extra):
    lines = ["schema_version: 1", "kind: doc-index", f"target: {target}", f"title: {title}", f"order: {order}",
             "links:", "  - text: doc", "    href: docs/x.md"]
    lines += [f"{k}: {v}" for k, v in extra.items()]
    return "\n".join(lines) + "\n"


@pytest.fixture
def repo(tmp_path):
    (tmp_path / "docs" / "fragments").mkdir(parents=True)
    (tmp_path / "README.md").write_bytes(README.encode())
    (tmp_path / "docs" / "modules.md").write_bytes(MODULES.encode())
    return tmp_path


def add(repo, name, text):
    (repo / "docs" / "fragments" / name).write_text(text, encoding="utf-8")


def region(repo):
    text = (repo / "README.md").read_text(encoding="utf-8")
    return text.split(BEGIN + "\n")[1].split(END)[0]


def test_build_renders_sorted_rows_and_leaves_outside_text(repo):
    add(repo, "2-b.yaml", frag("Beta", order=20))
    add(repo, "1-a.yaml", frag("Alpha", order=10))
    assert build.main(["--root", str(repo)]) == 0
    assert region(repo) == "| Topic | Doc |\n| --- | --- |\n| Alpha | [doc](docs/x.md) |\n| Beta | [doc](docs/x.md) |\n"
    text = (repo / "README.md").read_text(encoding="utf-8")
    assert text.startswith("intro text\n\n## Documentation\n\n" + BEGIN)
    assert text.endswith("\n\nafter text\n")
    assert "\n" + END + "\n\n" + COMP.split("\n")[0] in text


def test_output_is_deterministic_and_equal_order_ties_break_on_file_name(repo):
    add(repo, "9-z.yaml", frag("Zed", order=5))
    add(repo, "3-a.yaml", frag("Aye", order=5))
    build.main(["--root", str(repo)])
    first = (repo / "README.md").read_bytes()
    assert region(repo).index("Aye") < region(repo).index("Zed")
    assert build.build(repo) == []
    assert (repo / "README.md").read_bytes() == first


def test_crlf_files_keep_crlf(repo):
    (repo / "README.md").write_bytes(README.replace("\n", "\r\n").encode())
    add(repo, "1-a.yaml", frag("Alpha"))
    build.build(repo)
    data = (repo / "README.md").read_bytes()
    assert b"\n" not in data.replace(b"\r\n", b"")
    assert build.check(repo) == []


@pytest.mark.parametrize("text, message", [
    (frag().replace("schema_version: 1", "schema_version: 2"), "schema_version must be 1"),
    (frag().replace("doc-index", "bogus"), "kind must be one of"),
    (frag(target="nowhere"), "unknown target 'nowhere'"),
    (frag(title="a | b"), "title must be"),
    (frag(order="'x'"), "order must be an integer"),
    (frag(extra="1"), "unknown field(s) extra"),
    (frag().replace("    href: docs/x.md\n", ""), "links[0] must have exactly"),
    ("schema_version: 1\nkind: doc-index\ntarget: readme-docs-index\ntitle: T\n", "needs at least one link"),
    ("- a list\n", "must be a YAML mapping"),
    ("a: [unclosed\n", "not valid YAML"),
])
def test_schema_rejections_name_the_file_and_reason(repo, capsys, text, message):
    add(repo, "1-a.yaml", text)
    assert build.main(["--root", str(repo), "--check"]) == 1
    err = capsys.readouterr().err
    assert "1-a.yaml" in err and message in err


def test_bad_file_name_is_rejected(repo, capsys):
    add(repo, "Notes.yaml", frag())
    assert build.main(["--root", str(repo), "--check"]) == 1
    assert "<issue>-<slug>.yaml" in capsys.readouterr().err


def test_kind_must_match_target(repo):
    add(repo, "1-a.yaml", frag().replace("doc-index", "api-note"))
    assert "takes kind 'doc-index'" in build.check(repo)[0]


def test_check_passes_after_build_and_fails_on_hand_edit(repo, capsys):
    add(repo, "1-a.yaml", frag("Alpha"))
    build.build(repo)
    assert build.main(["--root", str(repo), "--check"]) == 0
    text = (repo / "README.md").read_text(encoding="utf-8")
    (repo / "README.md").write_text(text.replace("Alpha", "Alpha, hand edited"), encoding="utf-8")
    assert build.main(["--root", str(repo), "--check"]) == 1
    assert "edited by hand or is stale" in capsys.readouterr().err


def test_check_without_base_flags_new_fragment_not_yet_built(repo):
    add(repo, "1-a.yaml", frag("Alpha"))
    assert "stale" in build.check(repo)[0]


def git(repo, *args):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=repo, check=True,
                   capture_output=True)


def test_base_mode_allows_added_fragment_but_not_hand_edit(repo):
    add(repo, "1-a.yaml", frag("Alpha"))
    build.build(repo)
    git(repo, "init", "-q", "-b", "main")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "base")
    add(repo, "2-b.yaml", frag("Beta", order=20))
    assert build.check(repo, "main") == []          # new fragment, region untouched: passes
    assert build.check(repo) != []                  # but it is stale against its own fragments
    path = repo / "README.md"
    path.write_text(path.read_text(encoding="utf-8").replace("Alpha", "Edited"), encoding="utf-8")
    assert "edited by hand" in build.check(repo, "main")[0]
    build.build(repo)                               # regenerating on the branch is also accepted
    assert build.check(repo, "main") == []


def test_base_mode_with_unreadable_ref_falls_back_to_branch_fragments(repo, capsys):
    add(repo, "1-a.yaml", frag("Alpha"))
    build.build(repo)
    assert build.check(repo, "no-such-ref") == []
    assert "cannot read fragments at no-such-ref" in capsys.readouterr().err


def test_missing_or_duplicate_markers_fail(repo):
    (repo / "README.md").write_text("no markers\n", encoding="utf-8")
    assert "expected exactly one" in build.check(repo)[0]
    (repo / "README.md").write_text(README + README, encoding="utf-8")
    with pytest.raises(build.DocsError, match="found 2"):
        build.build(repo)


def test_repo_fragments_are_valid():
    # Regions are not compared here: after a fragment PR merges, main's region lags until docs-regen.yml runs.
    assert build.check(ROOT, fragments_only=True) == []
    assert len(list((ROOT / "docs" / "fragments").glob("*.yaml"))) >= 7


def test_fragments_only_ignores_stale_region_but_still_rejects_bad_fragments(repo):
    add(repo, "1-a.yaml", frag("Alpha"))
    assert build.main(["--root", str(repo), "--check", "--fragments-only"]) == 0
    add(repo, "2-b.yaml", "schema_version: 1\n")
    assert build.main(["--root", str(repo), "--check", "--fragments-only"]) == 1


def test_build_twice_changes_nothing_the_second_time(repo):
    add(repo, "1-a.yaml", frag("Alpha"))
    assert build.build(repo) == ["README.md", "docs/modules.md"]
    assert build.build(repo) == []


def test_option_like_ref_is_never_passed_to_git(repo):
    assert build.read_ref_fragments(repo, "--output=x") is None


MODULE = """schema_version: 1
kind: module
target: {target}
title: Images
summary: image generation
order: 10
links:
  - text: '`harness_modules/images/`'
    href: ../harness_modules/images/
"""


def test_module_fragments_render_list_and_components_table(repo):
    add(repo, "1-a.yaml", MODULE.format(target="modules-list"))
    add(repo, "2-b.yaml", MODULE.format(target="readme-components"))
    build.build(repo)
    modules = (repo / "docs" / "modules.md").read_text(encoding="utf-8")
    assert "- **Images** ([`harness_modules/images/`](../harness_modules/images/)): image generation" in modules
    readme = (repo / "README.md").read_text(encoding="utf-8")
    assert "| **Images** | image generation | [`harness_modules/images/`](../harness_modules/images/) |" in readme


@pytest.mark.parametrize("summary", ["", "a | b"])
def test_module_fragment_needs_a_plain_summary(repo, summary):
    add(repo, "1-a.yaml", MODULE.format(target="modules-list").replace("summary: image generation", f"summary: '{summary}'"))
    assert build.main(["--root", str(repo), "--check", "--fragments-only"]) == 1
