"""Failure paths for #264 review round 2: grammar load errors, missing pinned commit, empty report arms."""

import logging
import subprocess
import sys
import types

import pytest

from bakeoff import tasks_large
from bakeoff.repomap_report import arm_stats
from harness import repomap


@pytest.fixture(autouse=True)
def _fresh_parsers():
    repomap.reset_for_tests()
    yield
    repomap.reset_for_tests()


def _fake_tree_sitter(monkeypatch, language):
    ts = types.ModuleType("tree_sitter")
    ts.Language = language
    ts.Parser = lambda lang: object()
    monkeypatch.setitem(sys.modules, "tree_sitter", ts)
    for _, module, func, _d, _c in repomap._LANGUAGES.values():
        mod = types.ModuleType(module)
        setattr(mod, func, lambda: object())
        monkeypatch.setitem(sys.modules, module, mod)


def _raiser(exc):
    def language(grammar):
        raise exc
    return language


@pytest.mark.parametrize("exc", [ValueError("bad abi"), RuntimeError("incompatible"), OSError("dll"),
                                 AttributeError("no fn")])
def test_grammar_load_failure_disables_map(monkeypatch, tmp_path, caplog, exc):
    _fake_tree_sitter(monkeypatch, _raiser(exc))
    (tmp_path / "a.py").write_text("def f():\n    pass\n", encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger=repomap.log.name):
        assert repomap.build_map(tmp_path) == ""
        assert repomap.apply_to_prompt("PROMPT", tmp_path) == "PROMPT"
    assert len([r for r in caplog.records if "repo map disabled" in r.message]) == 1


def test_missing_grammar_function_disables_map(monkeypatch, tmp_path):
    _fake_tree_sitter(monkeypatch, lambda g: g)
    module = next(iter(repomap._LANGUAGES.values()))[1]
    monkeypatch.setitem(sys.modules, module, types.ModuleType(module))
    assert repomap.apply_to_prompt("PROMPT", tmp_path) == "PROMPT"


def test_pinned_commit_fetched_when_missing(monkeypatch):
    calls = []

    def fake_git(*args):
        calls.append(args[0])
        if args[0] == "cat-file" and calls.count("cat-file") == 1:
            raise subprocess.CalledProcessError(128, "git")
        return b""
    monkeypatch.setattr(tasks_large, "_git", fake_git)
    tasks_large._ensure_pinned_commit()
    assert calls == ["cat-file", "fetch", "cat-file"]


def test_pinned_commit_unfetchable_raises_clear_error(monkeypatch):
    def fake_git(*args):
        raise subprocess.CalledProcessError(128, "git", stderr=b"no such remote")
    monkeypatch.setattr(tasks_large, "_git", fake_git)
    tasks_large.checkout_files.cache_clear()
    with pytest.raises(tasks_large.PinnedCommitUnavailable, match="no such remote"):
        tasks_large.checkout_files()


def test_arm_stats_empty_names_context():
    with pytest.raises(SystemExit, match="runs/x.*model='typo'"):
        arm_stats([], " in runs/x (model='typo', suite=large)")
