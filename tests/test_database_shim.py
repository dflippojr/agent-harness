"""The core's domain stores sit behind a forwarding shim that may only shrink (#992 stage 5).

`Database` exposes each store as an attribute (`db.credentials`, `db.sessions`). Until their call sites move, the
shim forwards each old `db.<method>` name to its store; this file pins those names per store, so the shim never gains
one, and checks that a forwarded call (sync, `aio` or through `SessionStores`) runs the store's own method.
"""

from __future__ import annotations

import asyncio
import inspect
import time

import pytest

import harness.db as core_db
from harness.app_stores import SessionStores
from harness.credential_store import CredentialStore
from harness.db import CoreStore, Database
from harness.session_store import SessionStore

STORES: dict[str, type[CoreStore]] = {"credentials": CredentialStore, "sessions": SessionStore}

# The names `Database` forwards to each store. A store's set is written once, in the change that adds the store; after
# that, delete a name here once its last `db.<name>` caller has moved to the store, and never add one.
PINNED_SHIM: dict[str, frozenset[str]] = {
    "credentials": frozenset({
        # API keys
        "create_api_key", "api_key_by_secret", "get_api_key", "touch_api_key", "list_api_keys", "get_api_key_listing",
        "ensure_web_app", "revoke_api_key", "restore_api_key", "origin_allowed",
        # pairing codes
        "create_pairing_code", "list_pairing_codes", "revoke_pairing_code", "pairing_id_for_key",
        "pairing_origin_active", "redeem_pairing_code",
        # pairing requests and the Hub claim
        "insert_pairing_request", "get_pairing_request", "list_pairing_requests", "open_pairing_match_codes",
        "pending_pairing_catalog_origins", "pairing_request_load", "update_pairing_request", "expire_pairing_requests",
        "pairing_request_origin_known", "redeem_pairing_request", "hub_claim", "redeem_hub_claim", "release_hub",
        "hub_minted_owner_keys",
        # stream tickets
        "create_stream_ticket", "stream_ticket_key", "stream_ticket_origin_active",
        # App provider credentials
        "set_app_provider_credential", "app_provider_credential", "app_provider_credential_by_id",
        "app_provider_managed", "list_app_provider_credentials", "revoke_app_provider_credential",
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
    assert set(forwarded.values()) == set(PINNED_SHIM) == set(STORES), "a store's shim is pinned when it is added"
    for store, pinned in PINNED_SHIM.items():
        names = {n for n, s in forwarded.items() if s == store}
        assert shim_problems(names, pinned, _store_methods(STORES[store]), store) == [], store


def test_the_pinned_shim_has_these_sizes():
    """The set as this change leaves it: 39 credential names and 51 session names."""
    assert {store: len(names) for store, names in PINNED_SHIM.items()} == {"credentials": 39, "sessions": 51}
    assert not PINNED_SHIM["credentials"] & PINNED_SHIM["sessions"]


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
            # A query is `_reads` or `_writes`; a composite of them (`read_artifact`) carries no kind.
            assert callable(method) and getattr(method, "db_kind", "composite") in ("read", "write", "composite"), name
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
        assert await db.aio.revoke_api_key(row["id"]) is True
        return row
    row = asyncio.run(run())
    assert db.get_api_key(row["id"])["revoked_at"] is not None


def test_an_instance_patch_shadows_the_forwarded_name(db, monkeypatch):
    monkeypatch.setattr(db, "touch_api_key", lambda kid: "patched")
    assert db.touch_api_key("k-1") == "patched"
    monkeypatch.undo()
    assert db.touch_api_key.__self__ is db.credentials


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
        # The main store's SessionStore would miss the routed session, so SessionStores does not hand it out.
        with pytest.raises(AttributeError, match="routes each session"):
            stores.sessions  # noqa: B018 - the access itself is the check
    finally:
        stores.close()
