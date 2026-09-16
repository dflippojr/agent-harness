"""Embedding endpoint coverage kept separate from the actively edited UI/API test files."""

import httpx
from fastapi.testclient import TestClient

from harness.api import create_app
from harness.config import EndpointConfig
from harness.manager import Manager
from test_daemon import Completion, Script, make_cfg


def embedding_client(tmp_path, handler, configured=True):
    cfg = make_cfg(tmp_path)
    cfg.endpoint = EndpointConfig(
        enabled=True,
        embedding_base_url="http://embedding.test" if configured else "",
        embedding_model="nomic-embed-text-v1.5" if configured else "",
        embedding_context_tokens=8192,
    )
    manager = Manager(cfg, chat=Script([Completion(content="done")]))
    manager.endpoint_transport = httpx.MockTransport(handler)
    return TestClient(create_app(manager)), manager


def inference_key(client):
    return client.post("/keys", json={"name": "embedding-test"}).json()["key"]


def test_embeddings_are_hidden_until_a_dedicated_server_is_configured(tmp_path):
    client, _ = embedding_client(tmp_path, lambda request: httpx.Response(500), configured=False)
    with client:
        key = inference_key(client)
        auth = {"Authorization": f"Bearer {key}"}
        capabilities = client.get("/v1/capabilities", headers=auth).json()
        assert capabilities["features"]["embeddings"] is False
        assert "/v1/embeddings" not in capabilities["routes"]
        response = client.post("/v1/embeddings", headers=auth, json={"input": "hello"})
        assert response.status_code == 404
        assert "dedicated embedding server" in response.json()["error"]["message"]


def test_embeddings_proxy_to_the_dedicated_model_and_are_accounted(tmp_path):
    seen = []

    def embedding_server(request: httpx.Request):
        seen.append((str(request.url), request.read()))
        return httpx.Response(200, json={
            "object": "list",
            "data": [{"object": "embedding", "index": 0, "embedding": [0.25, -0.5]}],
            "model": "nomic-embed-text-v1.5",
            "usage": {"prompt_tokens": 3, "total_tokens": 3},
        })

    client, manager = embedding_client(tmp_path, embedding_server)
    with client:
        assert client.post("/v1/embeddings", json={"input": "hello"}).status_code == 401
        key = inference_key(client)
        auth = {"Authorization": f"Bearer {key}"}

        capabilities = client.get("/v1/capabilities", headers=auth).json()
        assert capabilities["features"]["embeddings"] is True
        assert capabilities["routes"]["/v1/embeddings"]["api"] == "openai"
        assert any(row["id"] == "nomic-embed-text-v1.5" for row in capabilities["models"])
        models = client.get("/v1/models", headers=auth).json()["data"]
        assert any(row["id"] == "nomic-embed-text-v1.5" and row["context_length"] == 8192 for row in models)

        response = client.post("/v1/embeddings", headers=auth,
                               json={"model": "text-embedding-3-small", "input": ["hello"], "stream": True})
        assert response.status_code == 200
        assert response.json()["data"][0]["embedding"] == [0.25, -0.5]
        assert seen[0][0] == "http://embedding.test/v1/embeddings"
        forwarded = __import__("json").loads(seen[0][1])
        assert forwarded["model"] == "nomic-embed-text-v1.5"
        assert "stream" not in forwarded

        row = manager.db.conn.execute(
            "SELECT route, model, stream, status, prompt_tokens, completion_tokens FROM endpoint_requests"
        ).fetchone()
        assert tuple(row) == ("/v1/embeddings", "nomic-embed-text-v1.5", 0, 200, 3, 0)
