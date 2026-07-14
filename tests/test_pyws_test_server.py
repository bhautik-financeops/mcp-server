import importlib
import os

import pytest

os.environ.setdefault("PYWS_BASE_URL", "http://localhost:8080")
os.environ.setdefault("PYWS_API_KEY", "test-masked-key")
os.environ.setdefault("PYWS_REPO_DIR", os.path.dirname(os.path.dirname(__file__)))

server = importlib.import_module("pyws_test_server")


def test_load_openapi_has_paths():
    spec = server._load_openapi()
    assert "paths" in spec
    assert len(spec["paths"]) > 50


def test_list_endpoints_filters():
    spec = server._load_openapi()
    rows = server._list_endpoints(spec, "rule-engine")
    assert rows
    assert all("rule-engine" in r["path"] for r in rows)
    assert {"path", "method"} <= set(rows[0])


def test_describe_endpoint_returns_methods_and_body():
    spec = server._load_openapi()
    desc = server._describe_endpoint(spec, "/api/ai/v1/check-send/predictions")
    assert desc["path"] == "/api/ai/v1/check-send/predictions"
    assert "POST" in [op["method"] for op in desc["operations"]]


def test_describe_endpoint_unknown_path_raises():
    spec = server._load_openapi()
    with pytest.raises(ValueError):
        server._describe_endpoint(spec, "/no/such/path")


class _FakeResponse:
    def __init__(self, status, json_body=None, text=""):
        self.status_code = status
        self._json = json_body
        self.text = text

    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json


def test_do_request_injects_auth_header(monkeypatch):
    captured = {}

    def fake_request(method, url, headers=None, json=None, params=None, timeout=None):
        captured["method"] = method
        captured["url"] = url
        captured["headers"] = headers
        captured["json"] = json
        return _FakeResponse(200, {"Response": {"code": 200}})

    monkeypatch.setattr(server.requests, "request", fake_request)
    result = server._do_request("POST", "/api/ai/v1/check-send/predictions",
                                {"Request": {"payload": []}}, None, 30)
    assert captured["headers"][server.API_KEY_HEADER] == os.environ["PYWS_API_KEY"]
    assert captured["url"].endswith("/api/ai/v1/check-send/predictions")
    assert result["ok"] is True
    assert result["status"] == 200


def test_do_request_non_2xx_not_ok(monkeypatch):
    monkeypatch.setattr(server.requests, "request",
                        lambda *a, **k: _FakeResponse(500, None, "boom"))
    result = server._do_request("POST", "/x", {}, None, 30)
    assert result["ok"] is False
    assert result["body"] == "boom"
