"""#500: API endpoint tables and the settings registry table are generated from code."""

import subprocess
import sys
import time
from collections import Counter
from pathlib import Path
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "docs"))
import build  # noqa: E402
import code_tables  # noqa: E402

from harness.settings import SettingSpec  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
FILES = {"app-api-endpoints": "docs/app-api.md", "admin-api-endpoints": "docs/admin-api.md",
         "config-registry-keys": "docs/config-registry.md"}

ROUTES = '''\
from harness.api import RouteTable

PREFIX = "/api/admin/v1"
ADMIN_PATHS = frozenset({"/widgets"})
web_router = RouteTable()
app_routes = RouteTable()


@app_routes.get("/api/v1/gadgets")
async def list_gadgets(request):
    """List the caller's gadgets.

    More detail that the table ignores.
    """
    auth(request, "sessions")


@app_routes.put("/api/v1/gadgets/{gid}")
async def wrapped(request, gid):
    """Replace one gadget, and say so
    over two source lines.

    Detail.
    """


@app_routes.post("/api/v1/gadgets", summary="Create | a gadget")
async def create_gadget(request):
    auth(request, "gadgets")


@app_routes.delete("/api/v1/gadgets/{gid}")
async def drop_gadget(request, gid):
    """Delete one gadget."""
    require_owner(request)


@web_router.get("/widgets")
async def widgets(request):
    """Owner widget list."""


def register(app):
    @app.get(PREFIX + "/direct")
    async def direct(request):
        """Served only on the admin surface."""
        require_admin(request, None)
'''


def spec(**kw):
    base = dict(key="fixture.limit", label="Limit", help="How many fixtures | at most.", category="Fixture",
                value_type="int", default=7, scope="admin", apply_mode="live", getter=lambda c: 1,
                setter=lambda c, v: None)
    return SettingSpec(**{**base, **kw})


@pytest.fixture
def repo(tmp_path, monkeypatch):
    (tmp_path / "harness").mkdir()
    (tmp_path / "harness" / "fixture_routes.py").write_text(ROUTES, encoding="utf-8")
    (tmp_path / "docs" / "fragments").mkdir(parents=True)
    regions = {name: rel for name, rel in FILES.items()} | {n: t.file for n, t in build.TARGETS.items()}
    for rel in sorted(set(regions.values())):
        body = "".join(f"<!-- generated:begin {n} -->\nold\n<!-- generated:end {n} -->\n" for n, f in regions.items()
                       if f == rel)
        (tmp_path / rel).write_bytes(f"intro\n\n{body}\noutro\n".encode())
    monkeypatch.setattr(code_tables, "load_specs", lambda root: [spec()])
    return tmp_path


def region(repo, name):
    text = (repo / FILES[name]).read_text(encoding="utf-8")
    return text.split(f"<!-- generated:begin {name} -->\n")[1].split("<!-- generated:end")[0]


def test_app_table_lists_methods_paths_auth_summary_and_source(repo):
    build.build(repo)
    rows = region(repo, "app-api-endpoints").splitlines()
    assert rows[0].startswith("| Method | Path | Auth | Summary | Source |")
    assert "| GET | `/api/v1/gadgets` | scope `sessions` | List the caller's gadgets. | " \
           "`harness/fixture_routes.py` `list_gadgets` |" in rows
    assert any("Replace one gadget, and say so over two source lines. |" in r for r in rows)
    assert any(r.startswith(r"| POST | `/api/v1/gadgets` | scope `gadgets` | Create \| a gadget |") for r in rows)
    assert any(r.startswith("| DELETE | `/api/v1/gadgets/{gid}` | owner | Delete one gadget. |") for r in rows)
    assert "/widgets" not in "".join(rows)


def test_admin_table_has_direct_routes_and_mirrors_admin_paths(repo):
    build.build(repo)
    text = region(repo, "admin-api-endpoints")
    assert "| GET | `/api/admin/v1/direct` | owner | Served only on the admin surface. |" in text
    assert "| GET | `/api/admin/v1/widgets` | owner (`admin` scope) | Owner widget list. |" in text
    assert "/api/v1/gadgets" not in text


def test_settings_table_uses_the_registry(repo):
    build.build(repo)
    assert region(repo, "config-registry-keys").splitlines()[2] == \
        r"| `fixture.limit` | int | `7` | admin | How many fixtures \| at most. |"


def test_settings_defaults_hide_machine_specific_values():
    assert code_tables.render_settings([spec(key="paths.data_dir", default="C:/x", apply_mode="installer_only")]) \
        .endswith(r"| `paths.data_dir` | int | — | admin | How many fixtures \| at most. |")
    assert "| — |" in code_tables.render_settings([spec(key="backends.claude.model", default="m")])


def test_output_is_deterministic(repo):
    build.build(repo)
    first = {name: region(repo, name) for name in FILES}
    assert build.build(repo) == []
    assert first == {name: region(repo, name) for name in FILES}


def test_check_fails_when_a_route_or_setting_is_missing_from_the_table(repo, monkeypatch):
    build.build(repo)
    assert build.check(repo) == []
    monkeypatch.setattr(code_tables, "load_specs", lambda root: [spec(), spec(key="fixture.other")])
    problems = build.check(repo)
    assert len(problems) == 1 and "config-registry-keys" in problems[0]
    src = repo / "harness" / "fixture_routes.py"
    src.write_text(ROUTES + '\n\n@app_routes.get("/api/v1/new")\nasync def new(request):\n    """New."""\n',
                   encoding="utf-8")
    assert {p.split(":")[0] for p in build.check(repo)} == {"docs/app-api.md", "docs/config-registry.md"}


def test_a_route_with_no_docstring_fails_the_check_and_is_named(repo, capsys):
    build.build(repo)
    assert build.check(repo) == []
    src = repo / "harness" / "fixture_routes.py"
    undocumented = '\n\n@app_routes.post("/api/v1/nodoc")\nasync def nodoc(request):\n    auth(request, "gadgets")\n'
    src.write_text(ROUTES + undocumented, encoding="utf-8")
    build.build(repo)
    problems = build.check(repo)
    assert len(problems) == 1 and problems[0].startswith("docs/app-api.md: ")
    assert "POST /api/v1/nodoc (harness/fixture_routes.py nodoc) has no summary or docstring" in problems[0]
    assert "warning" not in capsys.readouterr().err


def test_a_setting_with_no_help_text_is_only_a_warning(repo, monkeypatch, capsys):
    monkeypatch.setattr(code_tables, "load_specs", lambda root: [spec(help="")])
    build.build(repo)
    assert build.check(repo) == []
    assert "warning: setting fixture.limit has no help text" in capsys.readouterr().err


def git(repo, *args):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=repo, check=True,
                   capture_output=True)


def test_base_mode_accepts_an_untouched_table_but_not_a_hand_edit(repo):
    build.build(repo)
    git(repo, "init", "-q", "-b", "main")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "base")
    src = repo / "harness" / "fixture_routes.py"
    src.write_text(ROUTES + '\n\n@app_routes.get("/api/v1/new")\nasync def new(request):\n    """New."""\n',
                   encoding="utf-8")
    assert build.check(repo, "main") == []          # code changed, tables left to the post-merge job
    assert build.check(repo) != []                  # but on main itself they must match
    doc = repo / "docs" / "app-api.md"
    doc.write_text(doc.read_text(encoding="utf-8").replace("List the caller's", "Edited"), encoding="utf-8")
    assert "stale or was edited by hand" in build.check(repo, "main")[0]
    build.build(repo)                               # regenerating on the branch is accepted too
    assert build.check(repo, "main") == []


def test_build_reports_an_unresolvable_route_instead_of_a_traceback(repo, capsys):
    (repo / "harness" / "bad.py").write_text("@web_router.get(compute())\ndef f():\n    pass\n", encoding="utf-8")
    assert build.main(["--root", str(repo)]) == 1
    assert "harness/bad.py:1: cannot resolve the path" in capsys.readouterr().err


def test_a_local_name_bound_differently_in_two_functions_is_not_guessed(repo):
    src = ("def a(app):\n    prefix = '/api/v1/a'\n\n    @app.get(prefix + '/x')\n    def x():\n        pass\n\n\n"
           "def b(app):\n    prefix = '/api/v1/b'\n\n    @app.get(prefix + '/y')\n    def y():\n        pass\n")
    (repo / "harness" / "scoped.py").write_text(src, encoding="utf-8")
    with pytest.raises(code_tables.CodeTableError, match=r"harness/scoped.py:\d+: cannot resolve the path"):
        code_tables.collect_routes(repo)


def test_a_constant_defined_twice_in_a_routeless_file_is_not_guessed(repo):
    (repo / "harness" / "consts.py").write_text('BASE = "/api/v1/old"\nBASE = "/api/v1/new"\n', encoding="utf-8")
    (repo / "harness" / "user.py").write_text(
        "from .consts import BASE\n\n\n@app_routes.get(BASE + '/z')\ndef z():\n    pass\n", encoding="utf-8")
    with pytest.raises(code_tables.CodeTableError, match=r"harness/user.py:4: cannot resolve the path"):
        code_tables.collect_routes(repo)


def test_an_import_from_a_file_name_shared_by_two_modules_is_not_guessed(repo):
    for pkg, value in (("one", "/api/v1/one"), ("two", "/api/v1/two")):
        (repo / "harness" / pkg).mkdir()
        (repo / "harness" / pkg / "routes.py").write_text(f'BASE = "{value}"\n', encoding="utf-8")
    (repo / "harness" / "user.py").write_text(
        "from .one.routes import BASE\n\n\n@app_routes.get(BASE + '/z')\ndef z():\n    pass\n", encoding="utf-8")
    with pytest.raises(code_tables.CodeTableError, match=r"harness/user.py:4: cannot resolve the path"):
        code_tables.collect_routes(repo)


def test_an_unresolvable_route_path_names_the_file_and_line(repo):
    (repo / "harness" / "bad.py").write_text('@web_router.get(compute())\ndef f():\n    pass\n', encoding="utf-8")
    with pytest.raises(code_tables.CodeTableError, match=r"harness/bad.py:1: cannot resolve the path"):
        code_tables.collect_routes(repo)


def test_every_code_region_exists_in_its_real_doc():
    for name, region in code_tables.CODE_REGIONS.items():
        assert f"<!-- generated:begin {name} -->" in (ROOT / region.file).read_text(encoding="utf-8")


def test_real_repo_tables_are_in_sync_and_cover_known_entries():
    assert build.check(ROOT) == []
    routes = {(r.method, r.path) for r in code_tables.collect_routes(ROOT)}
    assert ("POST", "/api/v1/sessions") in routes
    assert ("PATCH", "/api/admin/v1/config") in routes
    assert ("GET", "/api/admin/v1/jobs") in routes  # a module's admin_paths mirror
    keys = {s.key for s in code_tables.load_specs(ROOT)}
    assert {"sessions.max_turns", "app.capabilities"} <= keys


@pytest.mark.parametrize("file_count,routes_per_file,delay", [
    (1, 1, 0), (1, 24, 0), (4, 1, 0), (4, 24, 0), (4, 24, 2.1),
])
def test_route_generation_reads_and_parses_sources_once(tmp_path, monkeypatch, file_count, routes_per_file, delay):
    """Catch per-route rescans/reparses and parsing routeless files as the input grows.

    Count actual work, not elapsed time: the delayed case exceeds the old two-second
    budget without adding work, just as a descheduled CI worker would.
    """
    sources, route_sources, expected_routes = set(), set(), set()
    for directory in code_tables.ROUTE_DIRS:
        base = tmp_path / directory
        base.mkdir()
        constants = base / f"{directory}_constants.py"
        constants.write_text('BASE = "/api/v1"\n', encoding="utf-8")
        sources.add(constants)
        for index in range(file_count):
            path = base / f"routes_{index}.py"
            source = f"from .{constants.stem} import BASE\n\n"
            for route in range(routes_per_file):
                suffix = f"/{directory}/{index}/{route}"
                source += (f'@app_routes.get(BASE + "{suffix}")\n'
                           f'async def route_{route}(request):\n    """Fixture route."""\n\n')
                expected_routes.add(("GET", "/api/v1" + suffix))
            path.write_text(source, encoding="utf-8")
            sources.add(path)
            route_sources.add(path.relative_to(tmp_path).as_posix())

    reads, parses = Counter(), Counter()
    read_text, parse = Path.read_text, code_tables.ast.parse

    def counted_read(path, *args, **kwargs):
        reads[path] += 1
        if delay and path == min(sources):
            time.sleep(delay)
        return read_text(path, *args, **kwargs)

    def counted_parse(source, filename="<unknown>", *args, **kwargs):
        parses[filename] += 1
        return parse(source, filename, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", counted_read)
    monkeypatch.setattr(code_tables.ast, "parse", counted_parse)
    routes = code_tables.collect_routes(tmp_path)
    assert {(r.method, r.path) for r in routes} == expected_routes
    assert reads == Counter({path: 1 for path in sources})
    assert parses == Counter({name: 1 for name in route_sources})


def test_settings_generation_loads_config_and_builds_registry_once(monkeypatch):
    """Catch rebuilding the config/registry for each setting instead of sharing one load."""
    from harness import config, settings_keys

    load_config = config.load
    build_registry = Mock(wraps=settings_keys.build_registry)

    def counted_load(*args, **kwargs):
        cfg = load_config(*args, **kwargs)
        # Config validation builds its own registries; count the docs extraction separately.
        build_registry.reset_mock()
        return cfg

    load = Mock(wraps=counted_load)
    monkeypatch.setattr(config, "load", load)
    monkeypatch.setattr(settings_keys, "build_registry", build_registry)
    specs = code_tables.load_specs(ROOT)
    assert {"sessions.max_turns", "app.capabilities"} <= {s.key for s in specs}
    load.assert_called_once()
    build_registry.assert_called_once()
