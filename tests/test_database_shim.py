"""The core's domain stores sit behind a forwarding shim that may only shrink (#992 stage 5).

`Database` exposes each store as an attribute (`db.credentials`, `db.sessions`, `db.accounts`, `db.apps`, `db.usage`). Until their call sites move, the
shim forwards each old `db.<method>` name to its store; this file pins those names per store, so the shim never gains
one, and checks that a forwarded call (sync, `aio` or through `SessionStores`) runs the store's own method.
"""

from __future__ import annotations

import asyncio
import inspect
import sqlite3
import time
from pathlib import Path

import pytest

import harness.app_stores as app_stores
import harness.db as core_db
from harness.account_store import AccountStore
from harness.app_store import AppStore
from harness.app_stores import WEB_APP_ID, SessionStores
from harness.credential_store import CredentialStore
from harness.db import CoreStore, Database
from harness.manager import Manager
from harness.session_store import SessionStore
from harness.usage_store import UsageStore

STORES: dict[str, type[CoreStore]] = {"credentials": CredentialStore, "sessions": SessionStore,
                                      "accounts": AccountStore, "apps": AppStore, "usage": UsageStore}

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
    }),
}


# Stores that land with every caller moved, so `Database` forwards none of their names, and never will.
NO_SHIM = frozenset({"accounts", "apps", "usage"})


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
    store was added, 28 before #1032), 20 session names (51 before #992 stage 6 moved the checkpoint, artifact,
    review-comment, secret-dismissal, approval and smart-review callers, 29 before it moved the event and search
    callers), and no account, App or usage names."""
    forwarded = _forwarded()
    sizes = {store: sum(1 for s in forwarded.values() if s == store) for store in STORES}
    assert sizes == {"credentials": 6, "sessions": 20, "accounts": 0, "apps": 0, "usage": 0}
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
            # A query is `_reads` or `_writes`. The one composite forwarded (`read_artifact`, which calls others and
            # carries no kind) left the shim with its callers (#992 stage 6).
            assert callable(method), name
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


# The rows each App keeps in its own store beside its sessions, and their `SessionStore` methods (#992 stage 6,
# decisions 1 and 3). They were never forwarded: every caller reaches them through a `sessions` routing proxy.
APP_STORE_TABLES = ("app_tool_calls", "end_users", "idempotency_keys", "namespace_audit")
APP_STORE_NAMES = frozenset({"insert_app_tool_call", "get_app_tool_call", "app_tool_calls", "finish_app_tool_call",
                             "register_end_user", "end_users", "forget_end_user", "insert_idempotency_key",
                             "drop_expired_idempotency_keys", "find_idempotency_key", "insert_namespace_audit",
                             "prune_namespace_audit", "namespace_audit_page"})


def test_session_queries_left_the_database_class():
    source = inspect.getsource(Database)
    for table in ("sessions", "events", "artifacts", "approvals", "checkpoints", "review_comments",
                  "secret_dismissals", "smart_reviews"):
        assert f"INTO {table} " not in source and f"UPDATE {table} " not in source, table
    for table in APP_STORE_TABLES:
        for verb in ("INTO", "UPDATE", "FROM"):
            assert f"{verb} {table} " not in source, (verb, table)
    assert APP_STORE_NAMES <= _store_methods(SessionStore)
    for name in APP_STORE_NAMES:
        assert not hasattr(Database, name), f"Database.{name}: call db.sessions.{name} (or for_app(app_id).sessions)"
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
    event = db.sessions.insert_event("s1", "user_message", {"content": "find the walrus"})
    assert db.sessions.events("s1")[0]["seq"] == event["seq"]
    assert db.get_session.__self__ is db.sessions
    # The search index is the store's: an event is indexed in the same write.
    assert [r["session_id"] for r in db.sessions.search_events('"walrus"')] == ["s1"]
    assert asyncio.run(db.aio.get_session("s1"))["id"] == "s1"
    assert asyncio.run(db.aio.update_session("s1", status="failed")) is None
    assert db.get_session("s1")["status"] == "failed"


def test_each_store_keeps_its_own_sessions_behind_session_stores(tmp_path):
    main = Database(tmp_path / "state.sqlite")
    stores = SessionStores(main, tmp_path / "apps")
    try:
        stores.insert_session(_session("own"))
        stores.insert_session(_session("a1", "k-aaaa"))
        stores.sessions.insert_event("a1", "user_message", {"content": "hello"})
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
    (credentials, accounts, apps, usage) is the main store's on every route, an App's raw Database included. A store whose rows
    are routed per session (sessions) is never one Database's on a routing wrapper (it would miss the sessions routed
    to another, and an App's would outlive the wrapper's hold on it): each wrapper hands out its own routing proxy,
    and `core_store` refuses it, so a wrapper without one fails instead of misrouting. A Web route is Web's Database,
    whose `sessions` holds the owner's and members' sessions."""
    main = Database(tmp_path / "state.sqlite")
    stores = SessionStores(main, tmp_path / "apps")
    try:
        key, _ = stores.create_api_key("Owner", "admin", kind="owner")
        now = time.time()
        main.accounts.insert_account({"user_id": "u-1", "login": "alice@example.com", "role": "member",
                                      "display_name": "Alice", "enabled": 1, "disk_quota_bytes": 1,
                                      "max_running": 1, "max_queued": 2, "created_at": now, "updated_at": now})
        app, _ = stores.create_api_key("An App", kind="app")
        assert main.apps.set_app_limits(app["id"], {"max_running": 2})
        main.apps.set_app_settings(app["id"], 3, {"app.notify.completion": "never"})
        main.usage.record_usage("fake", "a1", app["id"], 10, 5, 0.0, "subscription")
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
        assert set(main_homed) == {"credentials", "accounts", "apps", "usage"} and routed == ["sessions"]
        for label, route in {**wrappers, **web_routes, **databases}.items():
            for name in main_homed:
                assert getattr(route, name) is getattr(main, name), (label, name)
            assert key["id"] in {k["id"] for k in route.credentials.list_api_keys()}, label
            assert route.get_api_key(key["id"])["id"] == key["id"], label  # a forwarded credential call too
            assert route.accounts.account_by_login("alice@example.com")["user_id"] == "u-1", label
            assert route.apps.app_limits(app["id"])["max_running"] == 2, label
            assert route.apps.get_app_settings(app["id"])["revision"] == 3, label
            assert route.usage.usage_tally("fake", 0, app["id"])["prompt_tokens"] == 10, label
        for label, wrapper in wrappers.items():
            for name in routed:
                assert isinstance(getattr(wrapper, name), app_stores._RoutedSessions), (label, name)
                with pytest.raises(AttributeError, match="routes each session"):
                    stores.core_store(name)
        # A Web route is Web's Database itself: its `sessions` is the store that holds the owner's sessions.
        for label, wrapper in web_routes.items():
            assert wrapper.sessions is stores.web.sessions, label
            assert wrapper.sessions.get_session("own")["id"] == "own", label
        # The wrappers' own session calls still route: the App's session is read from its store.
        assert stores.for_app("k-aaaa").get_session("a1")["id"] == "a1"
        assert stores.scope("k-aaaa").get_session("a1")["id"] == "a1"
    finally:
        stores.close()


def _count(db: Database, table: str) -> int:
    return db.read(lambda: db.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def _agent_session(sid: str, app_id: str = "") -> dict:
    return {**_session(sid, app_id), "kind": "agent"}


def test_the_routing_proxy_keeps_each_apps_rows_in_its_own_store(tmp_path):
    """#992 stage 6, decision 1: the rows an App keeps in its own store (tool calls, end users, idempotency keys and
    the namespace audit) are `SessionStore`'s, reached through each wrapper's `sessions` proxy. They land in the App's
    store, never the main store's or Web's, and the proxies read them back (sync and `aio`)."""
    from harness import audit_context, idempotency
    main = Database(tmp_path / "state.sqlite")
    stores = SessionStores(main, tmp_path / "apps")
    try:
        stores.insert_session(_agent_session("a1", "k-aaaa"))
        stores.insert_session(_agent_session("own"))
        app = stores.for_app("k-aaaa").sessions
        stores.for_session("a1").sessions.register_end_user("dana")
        app.register_end_user("dana")  # the same App's store on both routes: registering twice is harmless
        app.insert_namespace_audit("k-aaaa", "a1", audit_context.SYSTEM, "a1", "session", "session.create", "ok", {})
        app.insert_idempotency_key({"key_hash": "h1", "request_digest": "d1", "session_id": "a1"})
        stores.sessions.insert_app_tool_call("a1", "c1", "lookup", {"x": 1})  # by session id
        stores.sessions.insert_app_tool_call("own", "c1", "lookup", {"x": 2})
        assert stores.sessions.finish_app_tool_call("a1", "c1", "done", "42", True)

        app_db = stores._open["k-aaaa"].db
        for table in APP_STORE_TABLES:
            assert _count(app_db, table) == 1, table
            assert _count(main, table) == 0, table
            assert _count(stores.web, table) == (table == "app_tool_calls"), table  # only the owner's tool call
        assert app.end_users() == ["dana"]
        assert app.find_idempotency_key("h1", idempotency.clock())["session_id"] == "a1"
        assert asyncio.run(app.aio.namespace_audit_page("k-aaaa"))["items"][0]["action"] == "session.create"
        assert stores.sessions.get_app_tool_call("a1", "c1")["output"] == "42"
        assert asyncio.run(stores.sessions.aio.app_tool_calls("own"))[0]["args"] == {"x": 2}
        assert stores.for_app("").sessions is stores.web.sessions  # a Web route is Web's own store
        # A scope's proxy narrows lists and searches as the scope does.
        assert {s["id"] for s in stores.scope("k-aaaa").sessions.list_sessions()} == {"a1", "own"}
        assert {s["id"] for s in stores.scope("").sessions.list_sessions()} == {"own"}
        assert {s["id"] for s in stores.sessions.list_sessions()} == {"own"}
        assert stores.scope("").sessions.find_session_ids("a") == []
        assert stores.sessions.find_session_ids("a") == ["a1"]  # the daemon's own lookup reaches every App
    finally:
        stores.close()


def test_the_routing_proxy_refuses_what_it_cannot_route(tmp_path):
    main = Database(tmp_path / "state.sqlite")
    stores = SessionStores(main, tmp_path / "apps")
    try:
        stores.insert_session(_agent_session("a1", "k-aaaa"))
        with pytest.raises(AttributeError, match="SessionStore has no method 'create_api_key'"):
            stores.sessions.create_api_key  # noqa: B018 - the attribute access is what is refused
        # An App's own rows take no session id: the unscoped proxy (and a scope's) cannot tell whose they are.
        for proxy in (stores.sessions, stores.scope("k-aaaa").sessions):
            for name in ("end_users", "insert_namespace_audit", "find_idempotency_key"):
                with pytest.raises(AttributeError, match=r"for_app\(app_id\)\.sessions"):
                    getattr(proxy, name)
        # The stage 6 names were never forwarded: no wrapper hands them out, only its `sessions` proxy.
        for wrapper in (stores, stores.scope("k-aaaa"), stores.for_app("k-aaaa")):
            for name in ("get_app_tool_call", "end_users", "namespace_audit_page"):
                with pytest.raises(AttributeError):
                    getattr(wrapper, name)
        # A write about an erased App session is refused through the proxy too: its id now routes to Web's store.
        stores.delete_session("a1")
        with pytest.raises(sqlite3.ProgrammingError, match="was erased"):
            stores.sessions.insert_app_tool_call("a1", "c1", "lookup", {})
        assert _count(stores.web, "app_tool_calls") == 0
    finally:
        stores.close()


def test_the_proxys_routing_tables_cover_every_session_method():
    """Every `SessionStore` method that takes a session id first routes by it (or is one of SessionStores' own),
    since the main store holds no sessions once they are in Web's store; every such write is refused for an erased
    App session; and the App's own rows the proxy refuses unscoped are exactly the stage 6 names without a session id."""
    for name in sorted(app_stores._SESSION_METHODS - app_stores._OWN_SESSION_METHODS):
        method = getattr(SessionStore, name)
        params = list(inspect.signature(method).parameters)
        if params[1:2] == ["sid"]:
            assert name in app_stores._BY_SESSION, f"{name} takes a session id: add it to _BY_SESSION"
        if name in app_stores._BY_SESSION and getattr(method, "db_kind", None) == "write":
            assert name in app_stores._SESSION_WRITES, f"{name} writes: add it to _SESSION_WRITES"
    assert app_stores._BY_SESSION <= app_stores._SESSION_METHODS
    assert app_stores._SESSION_WRITES <= app_stores._BY_SESSION
    assert app_stores._APP_ROWS | {"insert_app_tool_call", "get_app_tool_call", "app_tool_calls",
                                   "finish_app_tool_call"} == APP_STORE_NAMES


def test_the_unscoped_proxy_runs_every_other_session_method_on_webs_store(tmp_path):
    """Once the sessions are in Web's store, the main store holds none: a session method that is neither
    SessionStores' own, by session id nor an App's row runs on Web's store (review r1: `active_sessions`)."""
    main = Database(tmp_path / "state.sqlite")
    stores = SessionStores(main, tmp_path / "apps")
    try:
        assert stores.web is not main
        rest = app_stores._SESSION_METHODS - app_stores._OWN_SESSION_METHODS - app_stores._BY_SESSION \
            - app_stores._APP_ROWS
        assert {"active_sessions", "build_search_index", "group_sessions"} <= rest
        for name in sorted(rest):
            assert getattr(stores.sessions, name).__self__ is stores.web.sessions, name
        stores.insert_session({**_agent_session("own"), "status": "running"})
        assert [s["id"] for s in stores.sessions.active_sessions("owner")] == ["own"]
        assert [s["id"] for s in stores.active_sessions("owner")] == ["own"]  # the forwarded name too
    finally:
        stores.close()


def test_the_proxy_counts_keep_the_stores_holding_filter(tmp_path):
    """SessionStores' index counts take SessionStore's `holding` too (review r1): it needs the rows, so it counts
    them as `count_stored_*` does."""
    main = Database(tmp_path / "state.sqlite")
    stores = SessionStores(main, tmp_path / "apps")
    try:
        stores.insert_session({**_agent_session("own"), "status": "running"})
        stores.insert_session({**_agent_session("a1", "k-aaaa"), "status": "running"})
        stores.insert_session({**_agent_session("a2", "k-aaaa"), "status": "queued"})
        assert stores.sessions.count_sessions("owner", "running", "queued") == 3
        assert stores.sessions.count_sessions("owner", "running", "queued", holding=True) == 2
        assert stores.sessions.count_sessions("owner", "running", "queued", holding=True) == \
            stores.count_stored_sessions("owner", "running", "queued", holding=True)
        assert stores.sessions.count_app_sessions("k-aaaa", "running", "queued") == 2
        assert stores.sessions.count_app_sessions("k-aaaa", "running", "queued", holding=True) == 1
        assert stores.for_app("k-aaaa").sessions.count_app_sessions("k-aaaa", "running", "queued", holding=True) == 1
    finally:
        stores.close()


def test_an_approval_inserted_on_an_apps_route_is_indexed(tmp_path):
    """`for_app(app_id).sessions.insert_approval` runs SessionStores' own insert, which indexes the approval and its
    token, so the unscoped proxy finds and decides it (review r1)."""
    main = Database(tmp_path / "state.sqlite")
    stores = SessionStores(main, tmp_path / "apps")
    try:
        stores.insert_session(_agent_session("a1", "k-aaaa"))
        stores.for_app("k-aaaa").sessions.insert_approval(
            {"id": "ap-1", "session_id": "a1", "tool_call_id": "c1", "tool": "write_file", "args": {"path": "x"},
             "reason": "ask"})
        approval = stores.sessions.get_approval("ap-1")
        assert approval is not None and approval["session_id"] == "a1"
        assert stores.sessions.approval_by_token(approval["token"])["id"] == "ap-1"
        assert stores.sessions.decide_approval("ap-1", "approved")
        assert stores.for_app("k-aaaa").sessions.get_approval("ap-1")["status"] == "approved"
        assert stores.web.sessions.get_approval("ap-1") is None
    finally:
        stores.close()


def test_an_app_approvals_decision_holds_its_store_as_a_write(tmp_path):
    """SessionStores decides an App approval on that App's store as a write, its kind read from `SessionStore` (the
    forwarders on `Database` go, #992 stage 6): from the main store's transaction it raises instead of writing past
    the hold, and the approval stays pending."""
    main = Database(tmp_path / "state.sqlite")
    stores = SessionStores(main, tmp_path / "apps")
    try:
        stores.insert_session(_agent_session("a1", "k-aaaa"))
        stores.sessions.insert_approval({"id": "ap-1", "session_id": "a1", "tool_call_id": "c1", "tool": "write_file",
                                         "args": {"path": "x"}, "reason": "ask"})
        with pytest.raises(RuntimeError, match="another store's transaction"):
            main.write(lambda: stores.sessions.decide_approval("ap-1", "approved"))
        assert stores.sessions.get_approval("ap-1")["status"] == "pending"
        assert stores.sessions.decide_approval("ap-1", "approved")
        assert stores.sessions.get_approval("ap-1")["status"] == "approved"
    finally:
        stores.close()


def test_has_tool_result_reads_the_sessions_own_store(tmp_path):
    """`has_tool_result` (whether a session's sandbox ran a tool before, for the "environment recreated" notice) once
    read the main store, which holds no sessions after the move into Web's store: it routes by session id now."""
    main = Database(tmp_path / "state.sqlite")
    stores = SessionStores(main, tmp_path / "apps")
    try:
        assert stores.web is not main
        for sid, app_id in (("own", ""), ("a1", "k-aaaa")):
            stores.insert_session(_agent_session(sid, app_id))
            assert not stores.sessions.has_tool_result(sid) and not stores.for_session(sid).sessions.has_tool_result(sid)
            stores.sessions.insert_event(sid, "tool_result", {"id": "t1", "output": "ok"})
            assert stores.sessions.has_tool_result(sid) and stores.for_session(sid).sessions.has_tool_result(sid), sid
    finally:
        stores.close()

def test_an_app_route_holds_its_store_per_call_and_writes_only_from_its_own_writer(tmp_path):
    main = Database(tmp_path / "state.sqlite")
    stores = SessionStores(main, tmp_path / "apps")
    try:
        stores.insert_session(_agent_session("a1", "k-aaaa"))
        register = stores.for_app("k-aaaa").sessions.register_end_user  # kept past any hold, as `to_thread` does
        assert stores.close_idle(time.monotonic() + 10_000) == ["k-aaaa"]
        register("dana")  # reopens the App's store for this call, then lets it go
        assert stores._open["k-aaaa"].users == 0
        assert stores.for_app("k-aaaa").sessions.end_users() == ["dana"]
        # A write to the App's store from the main store's transaction raises instead of waiting on its writer.
        with pytest.raises(RuntimeError, match="another store's transaction"):
            main.write(lambda: register("erin"))
        assert stores.for_app("k-aaaa").sessions.end_users() == ["dana"]
    finally:
        stores.close()


def _moved_session_names() -> frozenset[str]:
    """`SessionStore`'s methods that `Database` does not forward (#992 stage 6): the App's own rows that joined the
    store, and each name whose callers moved to a `sessions` proxy. It grows as the `sessions` pin shrinks."""
    return frozenset(_store_methods(SessionStore) - PINNED_SHIM["sessions"])


def test_every_call_of_a_moved_session_method_goes_through_a_sessions_proxy():
    """No production code calls or passes a `SessionStore` name that `Database` no longer forwards on anything but a
    `sessions` store or proxy (`<x>.sessions.<name>` or `<x>.sessions.aio.<name>`). Six of them (the approval reads
    and decision, `insert_approval`, `insert_smart_review` and `pending_approvals`) are also SessionStores' own
    overrides, so a call left on `m.db` would still run: only this scan refuses it."""
    moved = _moved_session_names()
    assert APP_STORE_NAMES <= moved and not moved & set(_forwarded())
    for package in _production_packages():
        assert _uses_off_the_store(package, "sessions", moved) == [], package.name


def test_the_session_scan_allows_the_store_and_its_aio_only(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "x.py").write_text(
        "from . import end_users\n"
        "async def f(db, store, sid):\n"
        "    db.sessions.app_tool_calls(sid)\n"                # on the proxy: fine
        "    await store.sessions.aio.namespace_audit_page('k')\n"  # its aio: fine
        "    end_users.LoginError\n"                          # the module of that name: fine
        "    db.app_tool_calls(sid)\n"                        # line 6: on the Database
        "    await store.aio.namespace_audit_page('k')\n"     # line 7: the wrapper's aio
        "    return db.for_app('k').end_users\n",             # line 8: a bound method of the wrapper
        encoding="utf-8")
    assert sorted(_uses_off_the_store(tmp_path / "pkg", "sessions", APP_STORE_NAMES)) == [
        "pkg/x.py:6 .app_tool_calls", "pkg/x.py:7 .namespace_audit_page", "pkg/x.py:8 .end_users"]


def test_the_session_scan_allows_a_managers_and_a_classs_own_methods(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "x.py").write_text(
        "class Stores:\n"
        "    def get_approval(self, aid):\n"
        "        return self.web.sessions.get_approval(aid)\n"
        "    def decide(self, aid):\n"
        "        return self.get_approval(aid)\n"          # the class's own override: fine
        "def f(m, sid):\n"
        "    m.checkpoints(sid)\n"                         # Manager.checkpoints, the listing: fine
        "    m.db.sessions.checkpoints(sid)\n"             # on the proxy: fine
        "    m.db.checkpoints(sid)\n"                      # line 9: on the wrapper
        "    m.get_approval('ap-1')\n"                     # line 10: Manager has no get_approval
        "    return m.db.get_approval('ap-1')\n",          # line 11: SessionStores' override, off the proxy
        encoding="utf-8")
    assert sorted(_uses_off_the_store(tmp_path / "pkg", "sessions", frozenset({"checkpoints", "get_approval"}))) == [
        "pkg/x.py:10 .get_approval", "pkg/x.py:11 .get_approval", "pkg/x.py:9 .checkpoints"]


def test_the_session_scan_allows_a_declared_data_attribute_only_uncalled(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "lane.py").write_text(
        "import asyncio\n"
        "class Run:\n"
        "    events: list[dict]\n"                         # a field of that name
        "def f(run, db, sid):\n"
        "    [e for e in run.events]\n"                    # the field, read: fine
        "    db.sessions.events(sid)\n"                    # on the proxy: fine
        "    db.events(sid)\n"                             # line 7: a call is never a field
        "    return run.events\n",                         # the field again: fine
        encoding="utf-8")
    (tmp_path / "pkg" / "other.py").write_text(
        "import asyncio\n"
        "def g(m, sid):\n"
        "    return asyncio.to_thread(m.db.events, sid)\n",  # line 3: no field here, a bound method
        encoding="utf-8")
    assert sorted(_uses_off_the_store(tmp_path / "pkg", "sessions", frozenset({"events"}))) == [
        "pkg/lane.py:7 .events", "pkg/other.py:3 .events"]


ACCOUNT_TABLES = ("accounts", "account_audit", "member_projects", "github_connections", "member_api_keys",
                  "google_identities", "google_link_invitations", "web_sessions")


def test_account_queries_left_the_database_class():
    source = inspect.getsource(Database)
    for table in ACCOUNT_TABLES:
        assert f"INTO {table} " not in source and f"UPDATE {table} " not in source, table
        assert f"FROM {table} " not in source, table
    for name in _store_methods(AccountStore):
        assert not hasattr(Database, name), f"Database.{name}: call db.accounts.{name}"


_NOT_PRODUCTION = frozenset({"tests", "node_modules", "build", "dist", "__pycache__"})


def _py_files(root: Path):
    """The Python files under `root`, skipping installed and generated trees (`node_modules`, a hidden `.venv`)."""
    for path in sorted(root.rglob("*.py")):
        if not any(part in _NOT_PRODUCTION or part.startswith(".") for part in path.relative_to(root).parts[:-1]):
            yield path


def _production_packages() -> list[Path]:
    """Every top-level folder of the checkout with Python in it, except the tests: `harness`, `harness_modules` and
    `scripts`, and also `bakeoff`, `ops`, `tools`, `sdk`, `.claude` and the rest. Stage 6 batch 2 (#992) found
    `bakeoff` calls that a scan of the first three missed."""
    repo = Path(core_db.__file__).resolve().parent.parent
    return [p for p in sorted(repo.iterdir()) if p.is_dir() and p.name not in _NOT_PRODUCTION
            and (p.name == ".claude" or not p.name.startswith(".")) and next(_py_files(p), None) is not None]


def test_the_scans_cover_every_package_with_python_in_it():
    names = {p.name for p in _production_packages()}
    assert {"harness", "harness_modules", "scripts", "bakeoff", "sdk", ".claude"} <= names
    assert not names & {"tests", ".venv", ".git"}


def _uses_off_the_store(root, store: str, names: frozenset[str] | None = None) -> list[str]:
    """Each use of a method name of core store `store` (or of `names`, some of them: a call, or a bound method handed
    to `to_thread` or `awrite`) not made on that store: `<x>.<store>.<name>` or `<x>.<store>.aio.<name>`. Also
    allowed: for apps and sessions, `self.<name>` inside a class that defines `<name>` itself (`Manager.app_limits`,
    the effective caps; SessionStores' own overrides); for sessions, `m.<name>` for a method of the Manager (`m`) of
    that name (`checkpoints`, the listing, and `add_review_comment`), and, uncalled, a data attribute that a class in
    the same file declares (`real_env_lane.SessionRun.events`, a field: `run.events`); for accounts, an `accounts`
    stand-in (`principal.resolve_human`'s) or `operation_audit`'s `_audit_store(db)`."""
    import ast
    cls = STORES[store]
    names = _store_methods(cls) if names is None else names
    own_file = Path(inspect.getsourcefile(cls)).name
    found = []
    for path in _py_files(root):
        if path.name == own_file:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        own_methods: dict[int, set[str]] = {}  # each node -> the methods its innermost enclosing class defines
        for cls_node in (n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)):  # outer classes first
            methods = {n.name for n in cls_node.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
            for inner in ast.walk(cls_node):
                own_methods[id(inner)] = methods
        # Data attributes the file's classes declare: a field (`events: list[dict]`) or `self.<name> = ...`.
        fields = {t.target.id for c in ast.walk(tree) if isinstance(c, ast.ClassDef)
                  for t in c.body if isinstance(t, ast.AnnAssign) and isinstance(t.target, ast.Name)}
        fields |= {t.attr for a in ast.walk(tree) if isinstance(a, (ast.Assign, ast.AnnAssign))
                   for t in (a.targets if isinstance(a, ast.Assign) else [a.target])
                   if isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name) and t.value.id == "self"}
        called = {id(c.func) for c in ast.walk(tree) if isinstance(c, ast.Call)}
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Attribute) and node.attr in names):
                continue
            on = node.value
            if isinstance(on, ast.Attribute) and on.attr == "aio":
                on = on.value
            if isinstance(on, ast.Attribute) and on.attr == store:
                continue
            if store in ("apps", "sessions") and isinstance(on, ast.Name) and on.id == "self" and \
                    node.attr in own_methods.get(id(node), ()):
                continue
            if store == "sessions" and isinstance(on, ast.Name) and on.id == "m" and node.attr in vars(Manager):
                continue
            if store == "sessions" and node.attr in fields and id(node) not in called:
                continue
            if store == "accounts" and ((isinstance(on, ast.Name) and on.id == "accounts") or (
                    isinstance(on, ast.Call) and isinstance(on.func, ast.Name) and on.func.id == "_audit_store")):
                continue
            found.append(f"{path.relative_to(root.parent).as_posix()}:{node.lineno} .{node.attr}")
    return found


def _account_uses_off_the_store(root) -> list[str]:
    return _uses_off_the_store(root, "accounts")


def test_every_account_call_goes_through_the_account_store():
    """No production code calls or passes an account method of a Database (it has none): only of `db.accounts`."""
    for package in _production_packages():
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


def test_every_app_call_goes_through_the_app_store():
    """No production code calls or passes an App-store method of a Database (it has none): only of `db.apps`."""
    for package in _production_packages():
        assert _uses_off_the_store(package, "apps") == [], package.name


def test_the_app_scan_allows_a_method_of_the_same_name_only_where_it_is_defined(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "manager.py").write_text(
        "class Manager:\n"
        "    def app_limits(self, app_id):\n"
        "        return self.db.apps.app_limits(app_id)\n"   # on the store: fine
        "    def cap(self, app_id):\n"
        "        return self.app_limits(app_id)\n"           # the Manager's own method: fine
        "    def stored(self, app_id):\n"
        "        return self.db.app_limits(app_id)\n",       # line 7: on the Database
        encoding="utf-8")
    (tmp_path / "pkg" / "other.py").write_text(
        "class Other:\n"
        "    def go(self, kid):\n"
        "        return self.mark_app_erased(kid)\n"         # line 3: no such method in this class
        "class Erasing:\n"
        "    def mark_app_erased(self, kid):\n"              # another class's method of that name
        "        return kid\n",
        encoding="utf-8")
    assert _uses_off_the_store(tmp_path / "pkg", "apps") == ["pkg/manager.py:7 .app_limits",
                                                             "pkg/other.py:3 .mark_app_erased"]


def test_the_account_scan_has_no_own_method_allowance(tmp_path):
    """Only the App scan allows `self.<name>`: a class defining `member_count` hides nothing from the account scan."""
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "x.py").write_text(
        "class Counter:\n"
        "    def member_count(self):\n"
        "        return 0\n"
        "class Other:\n"
        "    def go(self):\n"
        "        return self.member_count()\n",              # line 6
        encoding="utf-8")
    assert _account_uses_off_the_store(tmp_path / "pkg") == ["pkg/x.py:6 .member_count"]


def test_every_usage_call_goes_through_the_usage_store():
    """No production code calls or passes a usage method of a Database (it has none): only of `db.usage`."""
    for package in _production_packages():
        assert _uses_off_the_store(package, "usage") == [], package.name


def test_usage_queries_left_the_database_class():
    source = inspect.getsource(Database)
    for table in ("usage", "backend_usage"):
        for verb in ("INTO", "UPDATE", "FROM"):
            assert f"{verb} {table} " not in source, (verb, table)
    for name in _store_methods(UsageStore):
        assert not hasattr(Database, name), f"Database.{name}: call db.usage.{name}"


# The main store's per-App records `AppStore` owns: no `Database` query names them.
APP_RECORDS = ("app_settings", "app_limits:", "retention_days =", "erased_at =", "erase_after =")


def test_app_queries_left_the_database_class():
    source = inspect.getsource(Database)
    for record in APP_RECORDS:
        assert record not in source, record
    assert not hasattr(core_db, "APP_LIMITS_META") and not hasattr(core_db, "APP_LIMIT_KEYS")
    for name in _store_methods(AppStore):
        assert not hasattr(Database, name), f"Database.{name}: call db.apps.{name}"


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
