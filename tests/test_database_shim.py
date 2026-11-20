"""The core's domain stores sit behind a forwarding shim that may only shrink (#992 stage 5).

`Database` exposes each store as an attribute (`db.credentials`, `db.sessions`). Until their call sites move, the
shim forwards each old `db.<method>` name to its store; this file pins those names per store, so the shim never gains
one, and checks that a forwarded call (sync, `aio` or through `SessionStores`) runs the store's own method.
"""

from __future__ import annotations

import asyncio
import inspect
import time
from pathlib import Path

import pytest

import harness.app_stores as app_stores
import harness.db as core_db
from harness.account_store import AccountStore
from harness.app_stores import WEB_APP_ID, SessionStores
from harness.credential_store import CredentialStore
from harness.db import CoreStore, Database
from harness.session_store import SessionStore

STORES: dict[str, type[CoreStore]] = {"credentials": CredentialStore, "sessions": SessionStore,
                                      "accounts": AccountStore}

# The names `Database` forwards to each store. A store's set is written once, in the change that adds the store; after
# that, delete a name here once its last `db.<name>` caller has moved to the store, and never add one.
PINNED_SHIM: dict[str, frozenset[str]] = {
    "credentials": frozenset({
        # API keys: the listings that SessionStores overrides (requests, store metadata), and the three every test
        # mints and reads keys with
        "create_api_key", "api_key_by_secret", "get_api_key", "list_api_keys", "get_api_key_listing",
        "restore_api_key",
    }),
    "sessions": frozenset({
        # sessions
        "insert_session", "update_session", "patch_run", "unset_run", "get_session", "get_session_for_user",
        "find_session_ids", "list_sessions", "session_page", "active_sessions", "group_sessions",
        "sessions_with_status", "sessions_with_run_flag", "count_sessions", "count_app_sessions",
        "count_stored_sessions", "count_stored_app_sessions", "delete_session", "job_sessions", "session_activity",
        # checkpoints and artifacts
        "add_checkpoint", "checkpoints", "show_checkpoints_through", "delete_checkpoints", "put_artifact",
        "read_artifact", "full_artifact",
        # review comments and secret dismissals
        "add_review_comment", "list_review_comments", "add_secret_dismissal", "secret_dismissals",
        "delete_review_comments",
        # events and search
        "insert_event", "search_events", "session_brief", "events", "has_tool_result", "pushed_heads",
        "snippet_events", "session_card_bits", "last_event_seq",
        # approvals and smart reviews
        "insert_approval", "approval_by_token", "get_approval", "approval_for_call", "pending_approvals", "approvals",
        "decide_approval", "insert_smart_review", "smart_review_stats", "smart_reviews",
    }),
}


# Forwarded store methods that are not one query but call others (so they carry no `db_kind`): `aio` runs them in a
# worker thread. Every other forwarded name must be `_reads` or `_writes`.
COMPOSITES = frozenset({("sessions", "read_artifact")})


# Stores that land with every caller moved, so `Database` forwards none of their names, and never will.
NO_SHIM = frozenset({"accounts"})


def _forwarded() -> dict[str, str]:
    """{name: store attribute} for every shim entry on `Database`, read from the class itself."""
    return {name: value.store for name, value in vars(Database).items() if isinstance(value, core_db._Forwarded)}


def shim_problems(forwarded: set[str], pinned: frozenset[str], store_methods: set[str],
                  store: str = "credentials") -> list[str]:
    """Why one store's shim breaks its ratchet: it forwards a name the pin lacks (it grew), the pin names one it no
    longer forwards (shrink the pin), or it forwards a name the store lacks."""
    problems = []
    if added := sorted(forwarded - pinned):
        problems.append(f"the forwarding shim grew: {added}. Call the store (`db.{store}.<name>`) instead.")
    if removed := sorted(pinned - forwarded):
        problems.append(f"the shim no longer forwards {removed}: delete them from PINNED_SHIM[{store!r}], so they "
                        "cannot come back.")
    if missing := sorted(forwarded - store_methods):
        problems.append(f"the shim forwards {missing}, which the store does not define.")
    return problems


def _store_methods(cls: type) -> set[str]:
    return {n for n, v in vars(cls).items() if callable(v) and not n.startswith("_")}


def test_the_shim_only_shrinks():
    forwarded = _forwarded()
    # Every store either has a pinned shim or none at all, and one added without a shim never gains one.
    assert set(PINNED_SHIM) | NO_SHIM == set(STORES) and not set(PINNED_SHIM) & NO_SHIM
    # A pin whose store forwards nothing any more is dropped, its store moving to NO_SHIM.
    assert set(forwarded.values()) == set(PINNED_SHIM), "a shim for a store in NO_SHIM, or a pin with no shim left"
    for store, pinned in PINNED_SHIM.items():
        names = {n for n, s in forwarded.items() if s == store}
        assert shim_problems(names, pinned, _store_methods(STORES[store]), store) == [], store


def test_database_forwards_these_many_names_per_store():
    """The shim on `Database` as this change leaves it, read from the class itself: 6 credential names (39 when the
    store was added, 28 before this change), 51 session names and no account names."""
    forwarded = _forwarded()
    sizes = {store: sum(1 for s in forwarded.values() if s == store) for store in STORES}
    assert sizes == {"credentials": 6, "sessions": 51, "accounts": 0}
    assert sizes == {store: len(PINNED_SHIM.get(store, ())) for store in STORES}


def test_the_ratchet_allows_a_removal_and_refuses_growth_and_a_stale_pin():
    pinned = frozenset({"a", "b"})
    store = {"a", "b", "c"}
    assert shim_problems({"a", "b"}, pinned, store) == []
    # A caller moved: its name leaves the shim and the pin, and stays a store method.
    assert shim_problems({"a"}, frozenset({"a"}), store) == []
    grew = shim_problems({"a", "b", "c"}, pinned, store, "sessions")
    assert len(grew) == 1 and "grew: ['c']" in grew[0] and "db.sessions" in grew[0]
    stale = shim_problems({"a"}, pinned, store)
    assert len(stale) == 1 and "no longer forwards ['b']" in stale[0]
    assert shim_problems({"a", "d"}, frozenset({"a", "d"}), store) == [
        "the shim forwards ['d'], which the store does not define."]


def test_every_forwarded_name_is_a_store_method():
    for store, pinned in PINNED_SHIM.items():
        cls = STORES[store]
        for name in pinned:
            method = vars(cls).get(name)
            # A query is `_reads` or `_writes`; only the named composites (of such queries) carry no kind.
            assert callable(method), name
            if (store, name) in COMPOSITES:
                assert not hasattr(method, "db_kind"), f"{store}.{name} is a query now: drop it from COMPOSITES"
            else:
                assert method.db_kind in ("read", "write"), name
            # The class-level lookup `app_stores` routes by sees the store's decorated method.
            assert getattr(Database, name) is method, name
        # A store method is forwarded only by its own `_Forwarded` line on `Database`, never by default.
        assert pinned <= _store_methods(cls)
        assert issubclass(cls, CoreStore)


def test_credential_queries_left_the_database_class():
    source = inspect.getsource(Database)
    for table in ("api_keys", "pairing_codes", "pairing_requests", "hub_claim", "stream_tickets",
                  "app_provider_credentials"):
        assert f"INTO {table} " not in source, table
    assert not hasattr(core_db, "_API_KEY_LISTING")
    assert "_insert_api_key" not in vars(Database)


def test_session_queries_left_the_database_class():
    source = inspect.getsource(Database)
    for table in ("sessions", "events", "artifacts", "approvals", "checkpoints", "review_comments",
                  "secret_dismissals", "smart_reviews"):
        assert f"INTO {table} " not in source and f"UPDATE {table} " not in source, table
    for private in ("_index_event", "_session_user_id", "_count_sessions", "_active_sessions", "_build_search_index"):
        assert private not in vars(Database), private
    assert not hasattr(core_db, "_session_filter") and not hasattr(core_db, "_SESSION_LIST_COLUMNS")


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "state.sqlite")
    try:
        yield database
    finally:
        database.close()


def test_a_forwarded_call_runs_on_the_databases_credential_store(db):
    assert isinstance(db.credentials, CredentialStore) and db.credentials.core is db
    row, secret = db.create_api_key("Device", "inference")
    assert db.credentials.api_key_by_secret(secret)["id"] == row["id"]
    assert db.api_key_by_secret(secret)["id"] == row["id"]
    assert db.list_api_keys() == db.credentials.list_api_keys()
    # A bound forwarded method is the store's own.
    assert db.get_api_key.__self__ is db.credentials


def test_aio_routes_a_forwarded_name_to_the_store(db, monkeypatch):
    """`await db.aio.<forwarded>(...)` runs the store's method with the store as `self`, on the writer queue."""
    seen: list[tuple] = []
    inner = vars(CredentialStore)["create_api_key"].__wrapped__

    def create_api_key(self, *args, **kwargs):
        seen.append((self, *args))
        return inner(self, *args, **kwargs)

    # The store's aio looks a write up on its class and sends the unwrapped function to the writer: patch the class.
    monkeypatch.setattr(CredentialStore, "create_api_key", core_db._writes(create_api_key))
    row, secret = asyncio.run(db.aio.create_api_key("App", "sessions", kind="app"))
    assert len(seen) == 1 and seen[0][0] is db.credentials and seen[0][1:] == ("App", "sessions")
    assert row["kind"] == "app" and db.api_key_by_secret(secret)["id"] == row["id"]


def test_aio_reads_and_writes_through_the_shim_join_the_writer(db):
    async def run():
        row, secret = await db.aio.create_api_key("Owner", "admin", kind="owner")
        assert await db.aio.api_key_by_secret(secret) == db.credentials.api_key_by_secret(secret)
        assert (await db.aio.get_api_key(row["id"]))["id"] == row["id"]
        assert await db.credentials.aio.revoke_api_key(row["id"]) is True  # a moved name: the store's own aio
        return row
    row = asyncio.run(run())
    assert db.get_api_key(row["id"])["revoked_at"] is not None


def test_an_instance_patch_shadows_the_forwarded_name(db, monkeypatch):
    monkeypatch.setattr(db, "get_api_key", lambda kid: "patched")
    assert db.get_api_key("k-1") == "patched"
    monkeypatch.undo()
    assert db.get_api_key.__self__ is db.credentials


def test_a_store_write_joins_the_callers_transaction(db):
    """A forwarded write inside `db.write` is part of that transaction: it rolls back with it."""
    def mint_then_fail():
        db.create_api_key("Rolled back", "inference")
        raise RuntimeError("abort")

    with pytest.raises(RuntimeError):
        db.write(mint_then_fail)
    assert [k["name"] for k in db.list_api_keys()] == []


def test_session_stores_forward_to_the_main_databases_store(tmp_path):
    main = Database(tmp_path / "state.sqlite")
    stores = SessionStores(main, tmp_path / "apps")
    try:
        assert stores.credentials is main.credentials
        row, _ = stores.create_api_key("App", "sessions", kind="app")
        listed = {k["id"]: k for k in stores.list_api_keys()}
        assert listed[row["id"]]["requests"] == 0  # SessionStores' own listing still adds the count
        assert "requests" not in {k["id"]: k for k in main.credentials.list_api_keys()}[row["id"]]
    finally:
        stores.close()


def _session(sid: str, app_id: str = "") -> dict:
    now = time.time()
    return {"id": sid, "project": "scratch", "target": "tower", "model": "fake", "title": sid, "status": "done",
            "workspace": "", "created_at": now, "updated_at": now, "context": [], "app_id": app_id,
            "owner_id": "owner"}


def test_a_forwarded_session_call_runs_on_the_databases_session_store(db):
    assert isinstance(db.sessions, SessionStore) and db.sessions.core is db
    db.insert_session(_session("s1"))
    event = db.insert_event("s1", "user_message", {"content": "find the walrus"})
    assert db.sessions.events("s1") == db.events("s1") and db.events("s1")[0]["seq"] == event["seq"]
    assert db.get_session.__self__ is db.sessions
    # The search index is the store's: an event is indexed in the same write.
    assert [r["session_id"] for r in db.search_events('"walrus"')] == ["s1"]
    assert asyncio.run(db.aio.get_session("s1"))["id"] == "s1"
    assert asyncio.run(db.aio.update_session("s1", status="failed")) is None
    assert db.get_session("s1")["status"] == "failed"


def test_each_store_keeps_its_own_sessions_behind_session_stores(tmp_path):
    main = Database(tmp_path / "state.sqlite")
    stores = SessionStores(main, tmp_path / "apps")
    try:
        stores.insert_session(_session("own"))
        stores.insert_session(_session("a1", "k-aaaa"))
        stores.insert_event("a1", "user_message", {"content": "hello"})
        app_db = stores._open["k-aaaa"].db
        assert app_db.sessions is not main.sessions and app_db.sessions.core is app_db
        assert app_db.sessions.get_session("a1")["app_id"] == "k-aaaa"
        assert [e["type"] for e in app_db.sessions.events("a1")] == ["user_message"]
        assert main.sessions.get_session("a1") is None and main.sessions.events("a1") == []
        assert stores.get_session("a1")["id"] == "a1" and stores.get_session("own")["id"] == "own"
    finally:
        stores.close()


def test_every_core_store_is_declared_and_says_where_its_rows_live():
    assert set(core_db.CORE_STORES) == set(STORES) == set(app_stores._STORE_HOME)
    for name, cls in STORES.items():
        assert core_db._store_class(name) is cls


def test_no_route_hands_out_the_wrong_databases_store(tmp_path):
    """Driven by `app_stores._STORE_HOME`, so each new store is covered: a store whose rows live in the main store
    (credentials, accounts) is the main store's on every route, an App's raw Database included. A store whose rows
    are routed per session (sessions) is refused by every routing wrapper: one Database's would miss the sessions
    routed to another, and an App's would outlive the wrapper's hold on it. A Web route is Web's Database, whose
    `sessions` holds the owner's and members' sessions."""
    main = Database(tmp_path / "state.sqlite")
    stores = SessionStores(main, tmp_path / "apps")
    try:
        key, _ = stores.create_api_key("Owner", "admin", kind="owner")
        now = time.time()
        main.accounts.insert_account({"user_id": "u-1", "login": "alice@example.com", "role": "member",
                                      "display_name": "Alice", "enabled": 1, "disk_quota_bytes": 1,
                                      "max_running": 1, "max_queued": 2, "created_at": now, "updated_at": now})
        stores.insert_session(_session("a1", "k-aaaa"))
        stores.insert_session(_session("own"))
        assert stores.web is not main  # the owner's sessions are in Web's own store
        wrappers = {"stores": stores, "scope(app)": stores.scope("k-aaaa"), "scope(owner)": stores.scope(""),
                    "for_app(app)": stores.for_app("k-aaaa"), "for_session(app sid)": stores.for_session("a1")}
        web_routes = {"for_app('')": stores.for_app(""), "for_app(WEB_APP_ID)": stores.for_app(WEB_APP_ID),
                      "for_session(owner sid)": stores.for_session("own")}
        databases = {"App Database": stores._open["k-aaaa"].db, "Web Database": stores.web}
        main_homed = [name for name, home in app_stores._STORE_HOME.items() if home == "main"]
        routed = [name for name, home in app_stores._STORE_HOME.items() if home is None]
        assert set(main_homed) == {"credentials", "accounts"} and routed == ["sessions"]
        for label, route in {**wrappers, **web_routes, **databases}.items():
            for name in main_homed:
                assert getattr(route, name) is getattr(main, name), (label, name)
            assert key["id"] in {k["id"] for k in route.credentials.list_api_keys()}, label
            assert route.get_api_key(key["id"])["id"] == key["id"], label  # a forwarded credential call too
            assert route.accounts.account_by_login("alice@example.com")["user_id"] == "u-1", label
        for wrapper in wrappers.values():
            for name in routed:
                with pytest.raises(AttributeError, match="routes each session"):
                    getattr(wrapper, name)
        # A Web route is Web's Database itself: its `sessions` is the store that holds the owner's sessions.
        for label, wrapper in web_routes.items():
            assert wrapper.sessions is stores.web.sessions, label
            assert wrapper.sessions.get_session("own")["id"] == "own", label
        # The wrappers' own session calls still route: the App's session is read from its store.
        assert stores.for_app("k-aaaa").get_session("a1")["id"] == "a1"
        assert stores.scope("k-aaaa").get_session("a1")["id"] == "a1"
    finally:
        stores.close()


ACCOUNT_TABLES = ("accounts", "account_audit", "member_projects", "github_connections", "member_api_keys",
                  "google_identities", "google_invitations", "web_sessions")


def test_account_queries_left_the_database_class():
    source = inspect.getsource(Database)
    for table in ACCOUNT_TABLES:
        assert f"INTO {table} " not in source and f"UPDATE {table} " not in source, table
        assert f"FROM {table} " not in source, table
    for name in _store_methods(AccountStore):
        assert not hasattr(Database, name), f"Database.{name}: call db.accounts.{name}"


def _account_uses_off_the_store(root) -> list[str]:
    """Each use of an AccountStore method name (a call, or a bound method handed to `to_thread` or `awrite`) not made
    on an account store: `<x>.accounts.<name>`, an `accounts` stand-in (`principal.resolve_human`'s), or
    `operation_audit`'s `_audit_store(db)`."""
    import ast
    names = _store_methods(AccountStore)
    found = []
    for path in sorted(root.rglob("*.py")):
        if path.name == "account_store.py":
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not (isinstance(node, ast.Attribute) and node.attr in names):
                continue
            on = node.value
            if (isinstance(on, ast.Attribute) and on.attr == "accounts") or \
                    (isinstance(on, ast.Name) and on.id == "accounts") or \
                    (isinstance(on, ast.Call) and isinstance(on.func, ast.Name) and on.func.id == "_audit_store"):
                continue
            found.append(f"{path.relative_to(root.parent)}:{node.lineno} .{node.attr}")
    return found


def test_every_account_call_goes_through_the_account_store():
    """No production code calls or passes an account method of a Database (it has none): only of `db.accounts`."""
    root = Path(core_db.__file__).resolve().parent
    for package in (root, root.parent / "harness_modules", root.parent / "scripts"):
        assert _account_uses_off_the_store(package) == [], package.name


def test_the_scan_finds_calls_and_bound_methods(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "x.py").write_text(
        "import asyncio\n"
        "def f(db, m, accounts):\n"
        "    db.accounts.list_audit(5)\n"          # on the store: fine
        "    accounts.member_count()\n"            # a stand-in named accounts: fine
        "    db.list_audit(5)\n"                   # line 5: a call on the Database
        "    return asyncio.to_thread(m.db.audit_page, 5)\n",  # line 6: a bound method
        encoding="utf-8")
    assert _account_uses_off_the_store(tmp_path / "pkg") == ["pkg/x.py:5 .list_audit", "pkg/x.py:6 .audit_page"]


def test_operation_audit_writes_through_the_account_store_or_a_stand_in(tmp_path):
    from harness import namespace_audit
    from harness.operation_audit import _audit_store
    main = Database(tmp_path / "state.sqlite")
    stores = SessionStores(main, tmp_path / "apps")
    try:
        stores.insert_session(_session("a1", "k-aaaa"))
        for label, db in {"Database": main, "SessionStores": stores, "scope(app)": stores.scope("k-aaaa"),
                          "for_app(app)": stores.for_app("k-aaaa"), "Web Database": stores.web}.items():
            assert _audit_store(db) is main.accounts, label
        login = namespace_audit.LoginStore(stores, "k-aaaa", "end-user")
        receipt = namespace_audit.ReceiptStore(stores, "login", "erase", 1)
        assert _audit_store(login) is login and _audit_store(receipt) is receipt
    finally:
        stores.close()
