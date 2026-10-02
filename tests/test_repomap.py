"""Repo map generator (#264). Needs the optional tree-sitter packages (requirements-repomap.txt)."""

import logging
import os
import subprocess
import sys

import pytest

from harness import repomap

pytest.importorskip("tree_sitter")
pytest.importorskip("tree_sitter_rust")
pytest.importorskip("tree_sitter_typescript")


@pytest.fixture(autouse=True)
def _fresh_parsers():
    repomap.reset_for_tests()
    yield
    repomap.reset_for_tests()


def write(root, files):
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")


PY_FIXTURE = {
    "core.py": "class Engine:\n    def run(self, job: str) -> int:\n        return 1\n\n    def stop(self):\n        pass\n\n"
               "def helper(x, y=2):\n    def nested(): pass\n    return x\n",
    "a.py": "from core import Engine, helper\n\ndef use_a():\n    Engine().run('a'); helper(1)\n",
    "b.py": "from core import Engine\n\ndef use_b():\n    return Engine()\n",
    "lonely.py": "def alone():\n    return 0\n",
}


def file_headers(text):
    return [line[:-1] for line in text.splitlines() if line.endswith(":") and not line.startswith(" ")]


def test_python_ranking_and_signatures(tmp_path):
    write(tmp_path, PY_FIXTURE)
    text = repomap.build_map(tmp_path)
    assert file_headers(text)[0] == "core.py"  # referenced by both others
    assert "  class Engine" in text
    assert "    def run(self, job: str) -> int" in text  # member, indented one level deeper
    assert "  def helper(x, y=2)" in text
    assert "nested" not in text  # only top level and members


def test_other_languages(tmp_path):
    write(tmp_path, {
        "lib.js": "export function load(path) { return path; }\nexport class Store { get(key) { return key; } }\n"
                  "const add = (a, b) => a + b;\n",
        "lib.ts": "export interface Opts { a: number }\nexport type Id = string;\n"
                  "export class Svc { run(o: Opts): Id { return ''; } }\n"
                  "export function make(o: Opts): Svc { return new Svc(); }\n",
        "view.tsx": "export function View(props: {a: number}) { return null; }\n",
        "main.go": "package main\n\ntype Server struct{}\n\nfunc (s *Server) Serve(addr string) error { return nil }\n\nfunc Run() {}\n",
        "lib.rs": "pub struct Cfg { a: u8 }\nimpl Cfg { pub fn new(a: u8) -> Cfg { Cfg { a } } }\n"
                  "pub trait Run { fn go(&self); }\npub fn start(c: Cfg) {}\n",
    })
    text = repomap.build_map(tmp_path, budget_tokens=5000)
    for expected in ("function load(path)", "class Store", "get(key)", "add(a, b)",
                     "interface Opts", "type Id", "class Svc", "function make(o: Opts): Svc", "function View(",
                     "type Server", "func (s *Server) Serve(addr string) error", "func Run()",
                     "struct Cfg", "impl Cfg", "fn new(a: u8) -> Cfg", "trait Run", "fn start(c: Cfg)"):
        assert expected in text, expected


def test_budget_trims_and_keeps_top_ranked(tmp_path):
    write(tmp_path, PY_FIXTURE)
    full = repomap.build_map(tmp_path, budget_tokens=5000)
    small = repomap.build_map(tmp_path, budget_tokens=30)
    assert repomap.estimate_tokens(small) <= 30
    assert full.startswith(small)
    assert small.startswith("core.py:")
    assert "lonely.py" in full and "lonely.py" not in small
    assert repomap.build_map(tmp_path, budget_tokens=0) == ""


def test_deterministic_and_tie_break_by_path(tmp_path):
    write(tmp_path, {f"{n}.py": f"def f_{n}():\n    pass\n" for n in ("z", "m", "a", "k")})
    first = repomap.build_map(tmp_path)
    assert file_headers(first) == ["a.py", "k.py", "m.py", "z.py"]
    for _ in range(3):
        assert repomap.build_map(tmp_path) == first


def test_pagerank_orders_by_incoming_references():
    ranks = repomap.pagerank(["a", "b", "c"], {"a": {"c": 1.0}, "b": {"c": 1.0}})
    assert ranks["c"] > ranks["a"] == ranks["b"]
    assert abs(sum(ranks.values()) - 1.0) < 1e-6
    assert repomap.pagerank([], {}) == {}


def test_skips_unparseable_other_language_large_and_ignored_dirs(tmp_path):
    write(tmp_path, {
        "ok.py": "def fine():\n    pass\n",
        "notes.txt": "def not_python(): pass\n",
        "node_modules/dep/x.js": "function hidden() {}\n",
        "venv/lib.py": "def venv_fn(): pass\n",
        "sub/__pycache__/c.py": "def cached(): pass\n",
    })
    (tmp_path / "binary.py").write_bytes(b"\xff\xfe\x00\x00 garbage ((((")
    (tmp_path / "big.py").write_text("def big():\n    pass\n" + "# pad\n" * 200_000, encoding="utf-8")
    assert (tmp_path / "big.py").stat().st_size > repomap.MAX_FILE_BYTES
    text = repomap.build_map(tmp_path)
    assert "fine" in text
    for absent in ("not_python", "hidden", "venv_fn", "cached", "big"):
        assert absent not in text


def test_uses_git_ls_files_and_respects_gitignore(tmp_path):
    if subprocess.run(["git", "--version"], capture_output=True).returncode != 0:
        pytest.skip("git unavailable")
    write(tmp_path, {"kept.py": "def kept(): pass\n", "ignored.py": "def ignored(): pass\n",
                     ".gitignore": "ignored.py\n"})
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "kept.py", ".gitignore"], cwd=tmp_path, check=True)
    text = repomap.build_map(tmp_path)
    assert "kept" in text and "ignored" not in text


def test_does_not_follow_symlinks_out_of_workspace(tmp_path):
    outside = tmp_path / "outside"
    ws = tmp_path / "ws"
    outside.mkdir()
    ws.mkdir()
    write(outside, {"secret.py": "def secret_fn(): pass\n"})
    write(ws, {"real.py": "def real_fn(): pass\n"})
    try:
        os.symlink(outside / "secret.py", ws / "link.py")
        os.symlink(outside, ws / "linkdir", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    text = repomap.build_map(ws)
    assert "real_fn" in text and "secret_fn" not in text


def test_never_executes_repository_code(tmp_path):
    marker = tmp_path / "EXECUTED"
    side_effect = f"open({str(marker)!r}, 'w').write('x')\n"
    write(tmp_path / "repo", {
        "evil.py": side_effect + "def evil(): pass\n",
        "setup.py": side_effect,
        "conftest.py": side_effect,
        "evil.js": f"require('fs').writeFileSync({str(marker)!r}, 'x');\nfunction evilJs() {{}}\n",
        "build.rs": f"fn main() {{ std::fs::write({str(marker)!r}, 1).unwrap(); }}\n",
    })
    text = repomap.build_map(tmp_path / "repo")
    assert "def evil()" in text and "evilJs" in text
    assert not marker.exists()


def test_missing_packages_warns_once_and_map_is_empty(tmp_path, monkeypatch, caplog):
    write(tmp_path, PY_FIXTURE)
    monkeypatch.setitem(sys.modules, "tree_sitter", None)  # import raises ImportError
    prompt = "You are an agent."
    with caplog.at_level(logging.WARNING, logger="harness.repomap"):
        assert repomap.build_map(tmp_path) == ""
        assert repomap.apply_to_prompt(prompt, tmp_path) == prompt
        assert repomap.build_map(tmp_path) == ""
    assert sum("repo map disabled" in r.message for r in caplog.records) == 1


def test_apply_to_prompt_adds_one_section_and_replaces_on_refresh(tmp_path):
    write(tmp_path, PY_FIXTURE)
    prompt = "You are an agent."
    once = repomap.apply_to_prompt(prompt, tmp_path)
    assert once.startswith(prompt) and once.count(repomap.SECTION_OPEN) == 1 and "class Engine" in once
    (tmp_path / "new.py").write_text("def brand_new(): pass\n", encoding="utf-8")
    again = repomap.apply_to_prompt(once, tmp_path)
    assert again.count(repomap.SECTION_OPEN) == 1 and "brand_new" in again and again.startswith(prompt)
    assert repomap.strip_section(again) == prompt


def test_empty_workspace_leaves_prompt_unchanged(tmp_path):
    assert repomap.apply_to_prompt("p", tmp_path) == "p"
