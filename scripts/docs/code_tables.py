"""Doc tables derived from code (#500): the API endpoint tables and the settings registry table.

Routes are read statically (AST) from the route registrations, so no daemon import is needed and the whole thing
runs in well under a second. The settings table imports the real registry (`harness.settings_keys.build_registry`)
under a pinned docs profile. Nothing here invents text: a route with no `summary=` and no docstring renders `TODO`,
and `build.py --check` lists those as warnings.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import os
import re
import sys
import tempfile
import types
from dataclasses import dataclass
from pathlib import Path

VERBS = ("get", "post", "put", "patch", "delete")
ROUTE_DIRS = ("harness", "harness_modules")
RECEIVER_SUFFIXES = ("_router", "_routes", "route_table")
APP_PREFIX = "/api/v1"
ADMIN_PREFIX = "/api/admin/v1"
OWNER_CALLS = {"require_admin", "require_owner", "require_admin_key"}
SCOPE_CALLS = {"auth", "app_auth"}
TODO = "TODO"

# A pinned profile so the generated defaults do not depend on a developer's config/harness.local.yaml.
DOCS_PROFILE = """\
listen: {host: 127.0.0.1, port: 8100}
default_model: fake
models: {fake: {base_url: http://unused, context_tokens: 1024}}
sandbox: {image: agent-harness-sandbox:py312}
backends: {claude: {enabled: true}, codex: {enabled: true}}
"""


class CodeTableError(Exception):
    """A route or setting could not be read; the message names the file."""


@dataclass(frozen=True)
class Route:
    method: str
    path: str
    auth: str
    summary: str
    file: str
    func: str
    receiver: str


def _py_files(root: Path):
    for top in ROUTE_DIRS:
        base = root / top
        if base.is_dir():
            yield from sorted(base.rglob("*.py"))


ROUTE_MARKER = re.compile(r"@\w+\.(?:get|post|put|patch|delete)\(|admin_paths|ADMIN_PATHS")


CONST_LINE = re.compile(r"""^([A-Z_][A-Z0-9_]*)\s*=\s*(['"])([^'"\\\n]*)\2\s*(?:#.*)?$""", re.M)


def _str_assignments(tree: ast.AST, deep: bool) -> dict[str, ast.expr]:
    """Name = expr assignments, anywhere in the file (function-local `prefix = ...` included). A name bound to
    different expressions maps to a non-string, so a route path built from it fails loudly instead of guessing."""
    found = {}
    for node in (ast.walk(tree) if deep else ast.iter_child_nodes(tree)):
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            name, prior = node.targets[0].id, found.get(node.targets[0].id)
            if prior is None:
                found[name] = node.value
            elif ast.dump(prior) != ast.dump(node.value):
                found[name] = ast.Constant(None)  # bound to different values in different scopes: do not guess
    return found


class _Consts:
    """Resolves the string expressions route paths are built from (`PREFIX + "/x"`, `prefix + ...`, `gs.NAME`)."""

    def __init__(self, trees: dict[str, ast.AST], route_files: set[str], plain: dict[str, str]):
        self.local = {name: _str_assignments(tree, True) for name, tree in trees.items()}
        for name, source in plain.items():  # files with no routes are not parsed; only their constant lines matter
            found: dict[str, ast.expr] = {}
            for m in CONST_LINE.finditer(source):
                prior = found.get(m[1])
                found[m[1]] = ast.Constant(m[3]) if prior is None or getattr(prior, "value", None) == m[3] else ast.Constant(None)
            self.local[name] = found
        merged: dict[str, set] = {}
        for exprs in self.local.values():
            for name, expr in exprs.items():
                if isinstance(expr, ast.Constant) and isinstance(expr.value, str):
                    merged.setdefault(name, set()).add(expr.value)
        self.imports = {}
        for file in route_files:
            for node in ast.walk(trees[file]):
                if isinstance(node, ast.ImportFrom) and node.module:
                    for alias in node.names:
                        self.imports.setdefault((file, alias.asname or alias.name), (node.module.split(".")[-1],
                                                                                      alias.name))
        self.shared = {name: next(iter(vals)) for name, vals in merged.items() if len(vals) == 1}

    def value(self, file: str, node: ast.expr, depth: int = 0) -> str | None:
        if depth > 6:
            return None
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            left, right = self.value(file, node.left, depth + 1), self.value(file, node.right, depth + 1)
            return None if left is None or right is None else left + right
        if isinstance(node, ast.JoinedStr):
            parts = []
            for part in node.values:
                piece = self.value(file, part.value if isinstance(part, ast.FormattedValue) else part, depth + 1)
                if piece is None:
                    return None
                parts.append(piece)
            return "".join(parts)
        if isinstance(node, ast.Name):
            expr = self.local.get(file, {}).get(node.id)
            if expr is not None and expr is not node:
                return self.value(file, expr, depth + 1)
            origin = self.imports.get((file, node.id))
            if origin:
                for other, exprs in self.local.items():
                    if Path(other).stem == origin[0] and origin[1] in exprs:
                        return self.value(other, exprs[origin[1]], depth + 1)
            return self.shared.get(node.id)
        if isinstance(node, ast.Attribute):
            return self.shared.get(node.attr)
        return None


def _first_paragraph(text: str | None) -> str:
    """The docstring's opening paragraph on one line, so a sentence wrapped over several lines is not cut short."""
    lines = []
    for line in (text or "").strip().splitlines():
        if not line.strip():
            break
        lines.append(line.strip())
    return " ".join(lines)


def _summary(fn: ast.FunctionDef | ast.AsyncFunctionDef, kwargs: list[ast.keyword]) -> str:
    for kw in kwargs:
        if kw.arg == "summary" and isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
            if kw.value.value.strip():
                return kw.value.value.strip()
    return _first_paragraph(ast.get_docstring(fn)) or TODO


def _call_name(call: ast.Call) -> str:
    func = call.func
    return func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else ""


def _auth(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    """What the handler itself asks for. Only direct calls are seen, so a helper's check reads as `see source`."""
    scopes, owner, dynamic = [], False, False
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call):
            continue
        name = _call_name(node)
        if name in OWNER_CALLS:
            owner = True
        elif name in SCOPE_CALLS:
            arg = node.args[1] if len(node.args) > 1 else None
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                scopes.append(arg.value)
            else:
                dynamic = True
    if owner and not scopes and not dynamic:
        return "owner"
    if scopes or dynamic:
        label = " / ".join(f"`{s}`" for s in sorted(set(scopes))) if scopes else "app token"
        return f"scope {label}" if scopes else label
    return "see source"


def _is_receiver(name: str) -> bool:
    return name == "app" or name.endswith(RECEIVER_SUFFIXES)


def _string_constants(node: ast.AST) -> set[str]:
    return {n.value for n in ast.walk(node) if isinstance(n, ast.Constant) and isinstance(n.value, str)}


def collect_routes(root: Path) -> list[Route]:
    """Every `@<receiver>.<verb>(path)` registration under harness/ and harness_modules/, in file order."""
    trees, route_files, plain = {}, set(), {}
    for path in _py_files(root):
        rel = path.relative_to(root).as_posix()
        source = path.read_text(encoding="utf-8")
        if not ROUTE_MARKER.search(source):
            plain[rel] = source
            continue
        route_files.add(rel)
        try:
            trees[rel] = ast.parse(source, filename=rel)
        except SyntaxError as exc:
            raise CodeTableError(f"{rel}: cannot parse ({exc.msg}, line {exc.lineno})") from exc
    consts = _Consts(trees, route_files, plain)
    routes, admin_paths = [], set()
    for rel in sorted(route_files):
        for node in ast.walk(trees[rel]):
            if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "ADMIN_PATHS"
                                                    for t in node.targets):
                admin_paths |= _string_constants(node.value)
            if isinstance(node, ast.Call):
                for kw in node.keywords:
                    if kw.arg == "admin_paths":
                        admin_paths |= _string_constants(kw.value)
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for deco in node.decorator_list:
                if not (isinstance(deco, ast.Call) and isinstance(deco.func, ast.Attribute)
                        and deco.func.attr in VERBS and isinstance(deco.func.value, ast.Name)
                        and _is_receiver(deco.func.value.id) and deco.args):
                    continue
                path = consts.value(rel, deco.args[0])
                if path is None:
                    raise CodeTableError(
                        f"{rel}:{deco.lineno}: cannot resolve the path of @{deco.func.value.id}.{deco.func.attr}; "
                        "build it from string literals and module constants")
                routes.append(Route(deco.func.attr.upper(), path, _auth(node), _summary(node, deco.keywords),
                                    rel, node.name, deco.func.value.id))
    return _surfaces(routes, admin_paths)


def _surfaces(routes: list[Route], admin_paths: set[str]) -> list[Route]:
    """Keep the two versioned API surfaces; mirror the unversioned owner routes the admin API also serves."""
    out = [r for r in routes if r.path.startswith(APP_PREFIX) or r.path.startswith(ADMIN_PREFIX)]
    for r in routes:
        if not r.path.startswith("/api") and r.path in admin_paths:
            out.append(Route(r.method, ADMIN_PREFIX + r.path, "owner (`admin` scope)", r.summary, r.file, r.func,
                             r.receiver))
    seen, unique = set(), []
    for r in out:
        if (r.method, r.path) not in seen:
            seen.add((r.method, r.path))
            unique.append(r)
    return unique


def _cell(text: str) -> str:
    return " ".join(text.split()).replace("|", "\\|")


def render_routes(routes: list[Route], surface: str) -> str:
    prefix = ADMIN_PREFIX if surface == "admin" else APP_PREFIX
    rows = sorted((r for r in routes if r.path == prefix or r.path.startswith(prefix + "/")),
                  key=lambda r: (r.path, r.method))
    lines = ["| Method | Path | Auth | Summary | Source |", "| --- | --- | --- | --- | --- |"]
    for r in rows:
        lines.append(f"| {r.method} | `{_cell(r.path)}` | {_cell(r.auth)} | {_cell(r.summary)} | "
                     f"`{r.file}` `{r.func}` |")
    return "\n".join(lines)


def route_warnings(routes: list[Route]) -> list[str]:
    return [f"{r.method} {r.path} ({r.file} {r.func}) has no summary or docstring; the table says {TODO}"
            for r in sorted(routes, key=lambda r: (r.path, r.method)) if r.summary == TODO]


# ---------- settings registry ----------

def load_specs(root: Path) -> list:
    """The real registry's specs under the pinned docs profile."""
    if importlib.util.find_spec("httpx") is None:
        # The config loader imports httpx at module level but loading a config never calls it; the post-merge
        # regeneration job installs only PyYAML, so give it an empty stand-in rather than a heavier install.
        sys.modules.setdefault("httpx", types.ModuleType("httpx"))
    root_str = str(root)
    if root_str not in sys.path:
        sys.path.insert(0, root_str)
    from harness.config import OPT_IN_MODULES, load
    from harness.settings_keys import build_registry
    with tempfile.TemporaryDirectory() as tmp:
        cfg_dir, data_dir = Path(tmp, "config"), Path(tmp, "data")
        cfg_dir.mkdir()
        data_dir.mkdir()
        optional = ", ".join(f"{name}: true" for name in sorted(OPT_IN_MODULES))  # off by default, so name them
        (cfg_dir / "harness.yaml").write_text(f"{DOCS_PROFILE}modules: {{{optional}}}\n", encoding="utf-8")
        env = {k: os.environ.pop(k) for k in ("HARNESS_CONFIG_DIR", "HARNESS_DATA_DIR") if k in os.environ}
        try:
            cfg = load(cfg_dir, data_dir)
            return list(build_registry(cfg).specs.values())
        finally:
            os.environ.update(env)


def _default(spec) -> str:
    # Hidden, path-like, installer-only and per-backend values depend on the machine, so they are never printed.
    from harness.settings import looks_hidden
    if spec.apply_mode == "installer_only" or looks_hidden(spec.key, spec) or spec.key.startswith("backends.") \
            or spec.default is None:
        return "—"
    return f"`{json.dumps(spec.default, sort_keys=True, default=str)}`"


def render_settings(specs: list) -> str:
    lines = ["| Key | Type | Default | Scope | Description |", "| --- | --- | --- | --- | --- |"]
    for spec in sorted(specs, key=lambda s: s.key):
        lines.append(f"| `{spec.key}` | {spec.value_type} | {_cell(_default(spec))} | {spec.scope} | "
                     f"{_cell(spec.help or TODO)} |")
    return "\n".join(lines)


def settings_warnings(specs: list) -> list[str]:
    return [f"setting {s.key} has no help text; the table says {TODO}" for s in sorted(specs, key=lambda s: s.key)
            if not s.help]


@dataclass(frozen=True)
class CodeRegion:
    file: str
    render: object  # (root) -> (body, warnings)


def _routes_region(surface: str):
    def render(root: Path) -> tuple[str, list[str]]:
        routes = collect_routes(root)
        return render_routes(routes, surface), route_warnings(
            [r for r in routes if r.path.startswith(ADMIN_PREFIX) == (surface == "admin")])
    return render


def _settings_region(root: Path) -> tuple[str, list[str]]:
    specs = load_specs(root)
    return render_settings(specs), settings_warnings(specs)


# region name -> where it lives and how it renders. Used by build.py for both writing and --check.
CODE_REGIONS = {
    "app-api-endpoints": CodeRegion("docs/app-api.md", _routes_region("app")),
    "admin-api-endpoints": CodeRegion("docs/admin-api.md", _routes_region("admin")),
    "config-registry-keys": CodeRegion("docs/config-registry.md", _settings_region),
}
