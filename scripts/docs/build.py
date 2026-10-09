"""Assemble generated doc regions from YAML fragments (#498).

A PR adds one fragment file under docs/fragments/ instead of editing a shared section. This script rewrites only
the text between `<!-- generated:begin NAME -->` and `<!-- generated:end NAME -->` in the target Markdown files.
Output is deterministic (stable sort, no timestamps) and uses only stdlib plus PyYAML. See docs/fragments/README.md.

    python scripts/docs/build.py                  write every generated region
    python scripts/docs/build.py --check          validate fragments; fail if a region is out of date
    python scripts/docs/build.py --check --base origin/main
                                                  PR mode: a region may match the base branch's fragments or this
                                                  branch's fragments, so a hand edit fails and a new fragment passes
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, NoReturn

import yaml

REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/@~^-]*$")
ROOT = Path(__file__).resolve().parent.parent.parent
FRAGMENTS_DIR = "docs/fragments"
SCHEMA_VERSION = 1
KINDS = ("module", "doc-index", "api-note", "operator-note")
NAME_RE = re.compile(r"^\d+-[a-z0-9]+(?:-[a-z0-9]+)*\.yaml$")
KEYS = {"schema_version", "kind", "target", "title", "summary", "links", "order"}
LINK_KEYS = {"text", "href"}


class DocsError(Exception):
    """A fragment, marker or target problem; the message names the file."""


@dataclass(frozen=True)
class Fragment:
    file: str
    kind: str
    target: str
    title: str
    summary: str
    links: tuple
    order: int


def _render_doc_index(frags: list[Fragment]) -> str:
    lines = ["| Topic | Doc |", "| --- | --- |"]
    for frag in frags:
        links = " · ".join(f"[{link['text']}]({link['href']})" for link in frag.links)
        lines.append(f"| {frag.title} | {links} |")
    return "\n".join(lines)


@dataclass(frozen=True)
class Target:
    file: str
    kind: str
    render: Callable[[list[Fragment]], str]


# target name -> where it renders and which fragment kind feeds it. Add a target here when a section is converted.
TARGETS = {
    "readme-docs-index": Target("README.md", "doc-index", _render_doc_index),
}


def parse_fragment(name: str, text: str) -> Fragment:
    """Validate one fragment's YAML text; raise DocsError with `name: reason` on the first problem."""

    def bad(msg: str) -> NoReturn:
        raise DocsError(f"{name}: {msg}")

    base = Path(name).name
    if not NAME_RE.match(base):
        bad("file name must be <issue>-<slug>.yaml (digits, a dash, then lowercase words joined by dashes)")
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        bad(f"not valid YAML ({exc})")
    if not isinstance(data, dict):
        bad("must be a YAML mapping")
    unknown = sorted(str(k) for k in set(data) - KEYS)
    if unknown:
        bad(f"unknown field(s) {', '.join(unknown)}; allowed: {', '.join(sorted(KEYS))}")
    if data.get("schema_version") != SCHEMA_VERSION:
        bad(f"schema_version must be {SCHEMA_VERSION}, got {data.get('schema_version')!r}")
    kind = data.get("kind")
    if kind not in KINDS:
        bad(f"kind must be one of {', '.join(KINDS)}, got {kind!r}")
    target = data.get("target")
    if target not in TARGETS:
        bad(f"unknown target {target!r}; known targets: {', '.join(sorted(TARGETS))}")
    if TARGETS[target].kind != kind:
        bad(f"target {target!r} takes kind {TARGETS[target].kind!r}, not {kind!r}")
    title = data.get("title")
    if not isinstance(title, str) or not title.strip() or "\n" in title or "|" in title:
        bad("title must be a non-empty single-line string without '|'")
    summary = data.get("summary", "")
    if not isinstance(summary, str):
        bad("summary must be a string")
    order = data.get("order", 0)
    if isinstance(order, bool) or not isinstance(order, int):
        bad(f"order must be an integer, got {order!r}")
    links = data.get("links", [])
    if not isinstance(links, list):
        bad("links must be a list of {text, href}")
    if kind == "doc-index" and not links:
        bad("a doc-index fragment needs at least one link")
    clean = []
    for i, link in enumerate(links):
        if not isinstance(link, dict) or set(link) != LINK_KEYS:
            bad(f"links[{i}] must have exactly the keys text and href")
        for key in LINK_KEYS:
            value = link[key]
            if not isinstance(value, str) or not value.strip() or "\n" in value or "|" in value:
                bad(f"links[{i}].{key} must be a non-empty single-line string without '|'")
        clean.append({"text": link["text"], "href": link["href"]})
    return Fragment(base, kind, target, title.strip(), summary.strip(), tuple(clean), order)


def load_fragments(texts: dict[str, str]) -> list[Fragment]:
    """Parse every fragment; report all problems at once."""
    frags, errors = [], []
    for name in sorted(texts):
        try:
            frags.append(parse_fragment(name, texts[name]))
        except DocsError as exc:
            errors.append(str(exc))
    if errors:
        raise DocsError("\n".join(errors))
    return frags


def read_worktree_fragments(root: Path) -> dict[str, str]:
    directory = root / FRAGMENTS_DIR
    if not directory.is_dir():
        return {}
    return {p.name: p.read_text(encoding="utf-8") for p in sorted(directory.glob("*.yaml"))}


def read_ref_fragments(root: Path, ref: str) -> dict[str, str] | None:
    """Fragments as committed at `ref`, or None when the ref cannot be read."""

    if not REF_RE.fullmatch(ref):
        return None

    def git(*args: str) -> str:
        return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True,
                              encoding="utf-8").stdout

    try:
        names = git("ls-tree", "--name-only", ref, f"{FRAGMENTS_DIR}/").splitlines()
        return {Path(n).name: git("show", f"{ref}:{n}") for n in sorted(names) if n.endswith(".yaml")}
    except (subprocess.CalledProcessError, OSError):
        return None


def render_regions(frags: list[Fragment]) -> dict[str, str]:
    """target name -> region body, for every known target (a target with no fragments renders its empty body)."""
    out = {}
    for name, target in TARGETS.items():
        mine = sorted((f for f in frags if f.target == name), key=lambda f: (f.order, f.file))
        out[name] = target.render(mine)
    return out


def _find_region(path: str, text: str, name: str) -> re.Match:
    pattern = re.compile(
        rf"(?P<begin><!-- generated:begin {re.escape(name)} -->\r?\n)(?P<body>.*?)"
        rf"(?P<end><!-- generated:end {re.escape(name)} -->)", re.S)
    found = list(pattern.finditer(text))
    if len(found) != 1:
        raise DocsError(f"{path}: expected exactly one generated:begin/end pair for {name!r}, found {len(found)}")
    return found[0]


def splice(path: str, text: str, name: str, body: str) -> str:
    m = _find_region(path, text, name)
    nl = "\r\n" if "\r\n" in m.group("begin") else "\n"
    return text[:m.start("body")] + body.replace("\n", nl) + nl + text[m.start("end"):]


def current_body(path: str, text: str, name: str) -> str:
    return _find_region(path, text, name).group("body").replace("\r\n", "\n").rstrip("\n")


def build(root: Path) -> list[str]:
    """Rewrite every generated region; return the files that changed."""
    bodies = render_regions(load_fragments(read_worktree_fragments(root)))
    changed = []
    for name, target in TARGETS.items():
        path = root / target.file
        text = path.read_bytes().decode("utf-8")
        new = splice(target.file, text, name, bodies[name])
        if new != text:
            path.write_bytes(new.encode("utf-8"))
            changed.append(target.file)
    return changed


def check(root: Path, base: str | None = None) -> list[str]:
    """Return problems: invalid fragments, or a region that matches no allowed rendering."""
    try:
        head = load_fragments(read_worktree_fragments(root))
    except DocsError as exc:
        return str(exc).splitlines()
    allowed = [("this branch's fragments", render_regions(head))]
    if base:
        base_texts = read_ref_fragments(root, base)
        if base_texts is None:
            print(f"warning: cannot read fragments at {base}; checking against this branch's fragments only",
                  file=sys.stderr)
        else:
            try:
                allowed.insert(0, (f"{base} fragments", render_regions(load_fragments(base_texts))))
            except DocsError as exc:
                return [f"fragments at {base} are invalid: {exc}"]
    problems = []
    for name, target in TARGETS.items():
        text = (root / target.file).read_bytes().decode("utf-8")
        try:
            have = current_body(target.file, text, name)
        except DocsError as exc:
            problems.append(str(exc))
            continue
        if all(have != bodies[name] for _, bodies in allowed):
            options = " or ".join(label for label, _ in allowed)
            problems.append(
                f"{target.file}: generated region {name!r} was edited by hand or is stale. It must equal what "
                f"{options} produce. Do not edit inside the markers; add a fragment under {FRAGMENTS_DIR}/ "
                f"(see {FRAGMENTS_DIR}/README.md), or run `python scripts/docs/build.py` to regenerate.")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true", help="validate fragments and region contents, write nothing")
    parser.add_argument("--base", help="with --check: git ref of the PR base; regions may match its fragments")
    parser.add_argument("--root", type=Path, default=ROOT, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.base and not args.check:
        parser.error("--base only applies with --check")
    try:
        if args.check:
            problems = check(args.root, args.base)
            for problem in problems:
                print(problem, file=sys.stderr)
            if not problems:
                print("docs fragments OK")
            return 1 if problems else 0
        changed = build(args.root)
    except DocsError as exc:
        print(exc, file=sys.stderr)
        return 1
    print("updated: " + ", ".join(changed) if changed else "docs already up to date")
    return 0


if __name__ == "__main__":
    sys.exit(main())
