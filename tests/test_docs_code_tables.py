"""#500: API endpoint tables and the settings registry table are generated from code."""

import subprocess
import sys
import time
from pathlib import Path

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
    readme = "x\n<!-- generated:begin readme-docs-index -->\nold\n<!-- generated:end readme-docs-index -->\n"
    (tmp_path / "README.md").write_bytes(readme.encode())
    for name, rel in FILES.items():
        (tmp_path / rel).write_bytes(f"intro\n\n<!-- generated:begin {name} -->\nold\n<!-- generated:end {name} -->\n"
                                     "\noutro\n".encode())
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
    assert any(r.startswith("| DELETE | `/api/v1/gadgets/{gid}` | owner | TODO |") for r in rows)
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


def test_missing_summary_is_a_warning_not_a_failure(repo, capsys):
    build.build(repo)
    assert build.check(repo) == []
    err = capsys.readouterr().err
    assert "warning: DELETE /api/v1/gadgets/{gid} (harness/fixture_routes.py drop_gadget)" in err
    assert "list_gadgets" not in err


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


def test_generation_stays_under_a_second():
    start = time.perf_counter()
    code_tables.collect_routes(ROOT)
    code_tables.load_specs(ROOT)
    assert time.perf_counter() - start < 2.0   # 1 s budget on a developer machine, doubled for shared runners
