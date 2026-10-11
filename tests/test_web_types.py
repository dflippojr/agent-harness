"""Generated-contract routes retain previews, additive data, and the native static assets."""

from pathlib import Path

from fastapi.testclient import TestClient
import pytest
from pydantic import ValidationError

from harness.api import create_app
from harness.api_models import SessionResponse, WebSessionResponse
from harness.manager import Manager
from test_daemon import make_cfg


ROOT = Path(__file__).parents[1]


def test_session_models_preserve_absent_and_additive_fields():
    payload = {
        "id": "s1", "project": "scratch", "target": "tower", "backend": "local", "model": "test",
        "title": "Task", "status": "done", "created_at": 1.0, "updated_at": 2.0,
        "totals": {}, "run": {"rate_limits": {"utilization": 0.25, "provider_field": "kept"}},
        "chat_summary": "Question — answer", "future_field": {"value": 1},
        "workspace_removed": 0,
    }
    assert WebSessionResponse.model_validate(payload).model_dump(exclude_unset=True) == payload
    expected = {"stop_reason": "", "answer": "", "app_tools": [], "metadata": {}, "failure": None,
                "last_event_seq": 0, "queue_position": None, **payload}
    assert SessionResponse.model_validate(payload).model_dump(exclude_unset=True) == expected
    without_counters = {key: value for key, value in payload.items() if key not in ("run", "totals")}
    defaulted = SessionResponse.model_validate(without_counters).model_dump(exclude_unset=True)
    assert defaulted["run"] == defaulted["totals"] == {}
    assert "pending_approvals" not in defaulted
    with pytest.raises(ValidationError):
        SessionResponse.model_validate([])
    # Legacy summaries contain tool objects, while the app surface exposes tool names.
    legacy = {**payload, "app_tools": [{"name": "lookup", "parameters": {}}]}
    assert WebSessionResponse.model_validate(legacy).model_dump(exclude_unset=True) == legacy
    for removed in (0, 1, False, True):
        dumped = SessionResponse.model_validate({**payload, "workspace_removed": removed}).model_dump(exclude_unset=True)
        assert type(dumped["workspace_removed"]) is type(removed)


def test_openapi_covers_web_contracts_and_static_serving_stays_native(tmp_path):
    manager = Manager(make_cfg(tmp_path))
    app = create_app(manager)
    app.state.manager = manager  # No application lifespan or services needed for schema/static reads.
    try:
        client = TestClient(app)
        try:
            schema = app.openapi()
            assert schema == app.openapi()  # Alias documentation is repeatable with FastAPI's schema cache.
            paths = schema["paths"]
            for path in ("/api/v1/sessions", "/api/v1/sessions/{ref}", "/api/admin/v1/sessions",
                         "/api/admin/v1/sessions/{ref}", "/api/admin/v1/jobs", "/api/admin/v1/jobs/{jid}",
                         "/api/admin/v1"):
                assert "$ref" in str(paths[path]["get"]["responses"]["200"])
            session = schema["components"]["schemas"]["SessionResponse"]["properties"]
            assert "chat_summary" in session
            assert "context_used" in session
            assert "pending_approvals" in session
            for path in ("/client.mjs", "/static/client.mjs", "/app.js", "/sw.js"):
                response = client.get(path)
                assert response.status_code == 200
                assert response.content == (ROOT / "harness/web" / Path(path).name).read_bytes()
            for path in ("/package.json", "/api.d.ts", "/openapi-schema.json", "/tools/web-types/package.json"):
                assert client.get(path).status_code == 404
        finally:
            client.close()
    finally:
        manager.db.close()
