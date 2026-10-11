"""Generated-contract routes retain previews, additive data, and the native static assets."""

from pathlib import Path

from fastapi.testclient import TestClient

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
    }
    for model in (SessionResponse, WebSessionResponse):
        assert model.model_validate(payload).model_dump(exclude_unset=True) == payload
    # Legacy summaries contain tool objects, while the app surface exposes tool names.
    legacy = {**payload, "app_tools": [{"name": "lookup", "parameters": {}}]}
    assert WebSessionResponse.model_validate(legacy).model_dump(exclude_unset=True) == legacy


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
