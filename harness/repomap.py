"""Ranked repository map (experiment, #264): a compact, PageRank-ordered symbol outline for a system prompt.

Parse only: files are read as bytes and handed to tree-sitter; nothing in the repository is imported, executed or
built. tree-sitter and the grammars (requirements-repomap.txt) are imported lazily, so nothing here costs anything
unless a caller asks for a map. If the packages are missing the map is empty and the prompt is left unchanged.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

DEFAULT_BUDGET_TOKENS = 1500
CHARS_PER_TOKEN = 4  # the chars/4 estimate the bake-off and efficiency metrics use for unmeasured text
MAX_FILE_BYTES = 1_000_000
MAX_SIGNATURE_CHARS = 120
SKIP_DIRS = frozenset({".git", "node_modules", "venv", ".venv", "__pycache__", "dist", "build"})
SECTION_OPEN = "<repo-map>"
SECTION_CLOSE = "</repo-map>"
_SECTION_RE = re.compile(r"\n\n" + re.escape(SECTION_OPEN) + r".*?" + re.escape(SECTION_CLOSE), re.DOTALL)

DAMPING = 0.85
PAGERANK_ITERATIONS = 100
PAGERANK_TOLERANCE = 1e-9

# language -> (extensions, grammar module, grammar function, definition node types, container node types)
_LANGUAGES = {
    "python": ((".py",), "tree_sitter_python", "language",
               {"class_definition": "class", "function_definition": "def"},
               {"class_definition"}),
    "javascript": ((".js", ".jsx", ".mjs", ".cjs"), "tree_sitter_javascript", "language",
                   {"class_declaration": "class", "function_declaration": "function",
                    "generator_function_declaration": "function", "method_definition": "method"},
                   {"class_declaration"}),
    "typescript": ((".ts",), "tree_sitter_typescript", "language_typescript",
                   {"class_declaration": "class", "function_declaration": "function", "method_definition": "method",
                    "interface_declaration": "interface", "type_alias_declaration": "type",
                    "enum_declaration": "enum", "abstract_class_declaration": "class"},
                   {"class_declaration", "abstract_class_declaration"}),
    "tsx": ((".tsx",), "tree_sitter_typescript", "language_tsx",
            {"class_declaration": "class", "function_declaration": "function", "method_definition": "method",
             "interface_declaration": "interface", "type_alias_declaration": "type",
             "enum_declaration": "enum", "abstract_class_declaration": "class"},
            {"class_declaration", "abstract_class_declaration"}),
    "go": ((".go",), "tree_sitter_go", "language",
           {"function_declaration": "func", "method_declaration": "func", "type_spec": "type"},
           set()),
    "rust": ((".rs",), "tree_sitter_rust", "language",
             {"function_item": "fn", "struct_item": "struct", "enum_item": "enum", "trait_item": "trait",
              "impl_item": "impl", "type_item": "type"},
             {"impl_item", "trait_item"}),
}
_EXT_TO_LANG = {ext: lang for lang, spec in _LANGUAGES.items() for ext in spec[0]}
_ARROW_VALUES = {"arrow_function", "function_expression", "function"}

_parsers: dict | None = None
_warned_missing = False


@dataclass
class Symbol:
    kind: str
    signature: str
    depth: int  # 0 top level, 1 member


@dataclass
class ParsedFile:
    path: str
    symbols: list[Symbol] = field(default_factory=list)
    defined: set[str] = field(default_factory=set)
    referenced: set[str] = field(default_factory=set)


def _load_parsers() -> dict | None:
    """One parser per language, or None (after one warning) when tree-sitter or a grammar isn't installed."""
    global _parsers, _warned_missing
    if _parsers is not None:
        return _parsers or None
    try:
        import importlib
        import tree_sitter
        parsers = {}
        for lang, (_, module, func, _defs, _containers) in _LANGUAGES.items():
            grammar = getattr(importlib.import_module(module), func)()
            parsers[lang] = tree_sitter.Parser(tree_sitter.Language(grammar))
    except ImportError as e:
        if not _warned_missing:
            _warned_missing = True
            log.warning("repo map disabled: tree-sitter packages missing (%s); install requirements-repomap.txt", e)
        _parsers = {}
        return None
    _parsers = parsers
    return parsers


def reset_for_tests() -> None:
    global _parsers, _warned_missing
    _parsers, _warned_missing = None, False


def estimate_tokens(text: str) -> int:
    return -(-len(text) // CHARS_PER_TOKEN)


def list_source_files(root: Path) -> list[str]:
    """Candidate files relative to root (posix), sorted: `git ls-files` in a git repo, else a pruned walk.

    Symlinks are never followed, and nothing resolving outside the workspace is returned."""
    root = Path(root)
    names = _git_files(root)
    if names is None:
        names = []
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
            names.extend(Path(dirpath, f).relative_to(root).as_posix() for f in filenames)
    real_root = root.resolve()
    out = []
    for rel in sorted(set(names)):
        if Path(rel).suffix.lower() not in _EXT_TO_LANG:
            continue
        path = root / rel
        try:
            if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(real_root):
                continue
            if path.stat().st_size > MAX_FILE_BYTES:
                continue
        except OSError:
            continue
        out.append(rel)
    return out


def _git_files(root: Path) -> list[str] | None:
    if not (root / ".git").exists():
        return None
    try:
        proc = subprocess.run(["git", "-c", "core.fsmonitor=false", "-C", str(root), "ls-files", "-z"],
                              capture_output=True, timeout=30, check=True)
    except (OSError, subprocess.SubprocessError):
        return None
    return [n for n in proc.stdout.decode("utf-8", "replace").split("\0") if n]


def _text(node, src: bytes) -> str:
    return src[node.start_byte:node.end_byte].decode("utf-8", "replace")


def _without_comments(node, src: bytes, end: int) -> str:
    """The source from `node` up to `end` minus comment nodes, so `#` or `//` inside a string is left alone."""
    spans = []
    stack = [node]
    while stack:
        cur = stack.pop()
        if "comment" in cur.type:
            spans.append((cur.start_byte, cur.end_byte))
        else:
            stack.extend(c for c in cur.children if c.start_byte < end)
    out, pos = [], node.start_byte
    for a, b in sorted(spans):
        if a >= end:
            break
        out.append(src[pos:a])
        pos = max(pos, min(b, end))
    out.append(src[pos:end])
    return b"".join(out).decode("utf-8", "replace")


def _signature(node, src: bytes) -> str:
    body = node.child_by_field_name("body")
    end = body.start_byte if body is not None else node.end_byte
    text = _without_comments(node, src, end)
    sig = " ".join(text.split())
    sig = sig.rstrip(":{ ").strip()
    if node.type == "type_spec":  # Go: the `type` keyword belongs to the enclosing type_declaration
        sig = "type " + sig
    if len(sig) > MAX_SIGNATURE_CHARS:
        sig = sig[:MAX_SIGNATURE_CHARS - 3].rstrip() + "..."
    return sig


def _definition_name(node, src: bytes) -> str | None:
    if node.type == "impl_item":
        target = node.child_by_field_name("type")
        return _text(target, src) if target is not None else None
    name = node.child_by_field_name("name")
    return _text(name, src) if name is not None else None


def _collect(node, src: bytes, defs: dict, containers: set, pf: ParsedFile, depth: int) -> None:
    """Walk the tree: record definitions (top level and one level of members) and every identifier reference."""
    stack = [(node, depth)]
    while stack:
        cur, d = stack.pop()
        if cur.child_count == 0:
            if cur.type.endswith("identifier"):
                pf.referenced.add(_text(cur, src))
            continue
        kind = defs.get(cur.type)
        child_depth = d
        if kind and d <= 1:
            name = _definition_name(cur, src)
            if name:
                pf.symbols.append(Symbol(kind, _signature(cur, src), d))
                if cur.type != "impl_item":
                    pf.defined.add(name)
                child_depth = d + 1 if cur.type in containers else 2
        elif cur.type == "variable_declarator":  # const f = () => ..., a JS/TS function binding
            value = cur.child_by_field_name("value")
            name = cur.child_by_field_name("name")
            if d == 0 and value is not None and name is not None and value.type in _ARROW_VALUES:
                pf.symbols.append(Symbol("function", _text(name, src) + _params(value, src), 0))
                pf.defined.add(_text(name, src))
        # reversed so the stack pops children in source order
        stack.extend((c, child_depth) for c in reversed(cur.children))


def _params(fn, src: bytes) -> str:
    params = fn.child_by_field_name("parameters")
    if params is None:
        param = fn.child_by_field_name("parameter")
        return f"({_text(param, src)})" if param is not None else "()"
    return " ".join(_text(params, src).split())


def parse_file(root: Path, rel: str, parsers: dict) -> ParsedFile | None:
    lang = _EXT_TO_LANG.get(Path(rel).suffix.lower())
    if lang is None:
        return None
    try:
        src = (Path(root) / rel).read_bytes()
        tree = parsers[lang].parse(src)
    except Exception:  # unreadable or unparseable: skipped silently
        return None
    if tree.root_node.has_error and not tree.root_node.children:
        return None
    pf = ParsedFile(rel)
    defs, containers = _LANGUAGES[lang][3], _LANGUAGES[lang][4]
    _collect(tree.root_node, src, defs, containers, pf, 0)
    return pf


def pagerank(nodes: list[str], edges: dict[str, dict[str, float]]) -> dict[str, float]:
    """Plain power-iteration PageRank. `edges[a][b]` is the weight of a -> b. Dangling mass spreads uniformly."""
    n = len(nodes)
    if n == 0:
        return {}
    rank = {p: 1.0 / n for p in nodes}
    out_weight = {p: sum(edges.get(p, {}).values()) for p in nodes}
    for _ in range(PAGERANK_ITERATIONS):
        dangling = sum(rank[p] for p in nodes if out_weight[p] == 0)
        base = (1.0 - DAMPING) / n + DAMPING * dangling / n
        new = {p: base for p in nodes}
        for a in nodes:
            if out_weight[a]:
                share = DAMPING * rank[a] / out_weight[a]
                for b, w in sorted(edges[a].items()):
                    new[b] += share * w
        delta = sum(abs(new[p] - rank[p]) for p in nodes)
        rank = new
        if delta < PAGERANK_TOLERANCE:
            break
    return rank


def rank_files(parsed: list[ParsedFile]) -> list[ParsedFile]:
    """Files in descending PageRank over the reference graph; ties break by path so output is stable."""
    definers: dict[str, list[str]] = {}
    for pf in parsed:
        for name in pf.defined:
            definers.setdefault(name, []).append(pf.path)
    edges: dict[str, dict[str, float]] = {}
    for pf in parsed:
        for name in pf.referenced & set(definers):
            targets = [t for t in definers[name] if t != pf.path]
            for t in targets:
                edges.setdefault(pf.path, {}).setdefault(t, 0.0)
                edges[pf.path][t] += 1.0 / len(targets)
    ranks = pagerank(sorted(pf.path for pf in parsed), edges)
    return sorted(parsed, key=lambda pf: (-round(ranks[pf.path], 12), pf.path))


def render(ranked: list[ParsedFile], budget_tokens: int) -> str:
    """Files in rank order, each with its symbols, cut off at the token budget (chars/4)."""
    budget_chars = max(0, budget_tokens) * CHARS_PER_TOKEN
    lines: list[str] = []
    used = 0
    for pf in ranked:
        if not pf.symbols:
            continue
        block = [f"{pf.path}:"] + [f"{'  ' * (s.depth + 1)}{s.signature}" for s in pf.symbols]
        cost = sum(len(line) + 1 for line in block)
        if used + cost <= budget_chars:
            lines.extend(block)
            used += cost
            continue
        # a partial block is only worth emitting when the header and at least one symbol fit
        fit = []
        for line in block:
            if used + len(line) + 1 > budget_chars:
                break
            fit.append(line)
            used += len(line) + 1
        if len(fit) > 1:
            lines.extend(fit)
        break
    return "\n".join(lines)


def build_map(root: Path | str, budget_tokens: int = DEFAULT_BUDGET_TOKENS) -> str:
    """The repo map text for a workspace, or "" if there is nothing to show or tree-sitter is unavailable."""
    parsers = _load_parsers()
    if not parsers:
        return ""
    root = Path(root)
    parsed = [pf for rel in list_source_files(root) if (pf := parse_file(root, rel, parsers)) is not None]
    return render(rank_files(parsed), budget_tokens)


def strip_section(prompt: str) -> str:
    return _SECTION_RE.sub("", prompt)


def apply_to_prompt(prompt: str, root: Path | str, budget_tokens: int = DEFAULT_BUDGET_TOKENS) -> str:
    """`prompt` with exactly one repo-map section for `root`. An empty map yields the prompt without any section,
    so a missing grammar or an empty workspace leaves the text unchanged. Calling it again replaces the section."""
    base = strip_section(prompt)
    text = build_map(root, budget_tokens)
    if not text:
        return base
    return (f"{base}\n\n{SECTION_OPEN}\nRanked map of the repository's files and symbols, most central first "
            f"(a starting point, not the full tree; use search and read_file for details):\n{text}\n{SECTION_CLOSE}")
