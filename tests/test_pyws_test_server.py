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


def test_health_survives_unreachable(monkeypatch):
    def boom(*args, **kwargs):
        raise server.requests.exceptions.ConnectionError("refused")

    monkeypatch.setattr(server.requests, "request", boom)
    out = server.health()
    assert out["ping"]["ok"] is False
    assert "refused" in out["ping"]["error"]
    assert out["platform"]["ok"] is False


def test_merge_clients_applies_overrides():
    mongo = [
        {"clientId": "A", "preferredMedium": "email"},
        {"clientId": "B", "preferredMedium": "sms"},
        {"clientId": "C", "preferredMedium": "email"},
    ]
    overrides = {
        "include_only": [],
        "exclude": ["C"],
        "clients": {"B": {"preferredMedium": "email", "sampleCustomerId": "cust-B"}},
    }
    merged = server._merge_clients(mongo, overrides)
    by_id = {c["clientId"]: c for c in merged}
    assert "C" not in by_id                       # excluded
    assert by_id["B"]["preferredMedium"] == "email"   # forced
    assert by_id["B"]["sampleCustomerId"] == "cust-B"
    assert by_id["A"]["source"] == "mongo"


def test_merge_clients_include_only_restricts():
    mongo = [{"clientId": "A", "preferredMedium": "email"},
             {"clientId": "B", "preferredMedium": "sms"}]
    overrides = {"include_only": ["A"], "exclude": [], "clients": {}}
    merged = server._merge_clients(mongo, overrides)
    assert [c["clientId"] for c in merged] == ["A"]


def test_merge_clients_adds_override_only_client():
    mongo = [{"clientId": "A", "preferredMedium": "email"}]
    overrides = {"include_only": [], "exclude": [],
                 "clients": {"Z": {"preferredMedium": "sms", "sampleCustomerId": "cz"}}}
    merged = server._merge_clients(mongo, overrides)
    by_id = {c["clientId"]: c for c in merged}
    assert by_id["Z"]["source"] == "override"
    assert by_id["Z"]["preferredMedium"] == "sms"


def test_build_client_payload_ai_envelope():
    base = {"Request": {"payload": [{"primaryKey": "1", "text": "hi"}]}}
    client = {"clientId": "A", "sampleCustomerId": "custA", "preferredMedium": "email"}
    body = server._build_client_payload(base, client, "communicationMedium")
    item = body["Request"]["payload"][0]
    assert item["clientId"] == "A"
    assert item["customerId"] == "custA"
    assert item["communicationMedium"] == "email"
    assert item["text"] == "hi"                       # untouched
    assert base["Request"]["payload"][0].get("clientId") is None  # deep-copied, no mutation


def test_build_client_payload_flat_core():
    base = {"someField": 1}
    client = {"clientId": "B", "sampleCustomerId": None, "preferredMedium": "sms"}
    body = server._build_client_payload(base, client, "communicationMedium")
    assert body["clientId"] == "B"
    assert body["communicationMedium"] == "sms"
    assert body["someField"] == 1


def test_fanout_isolates_failures(monkeypatch):
    monkeypatch.setattr(server, "_fetch_clients_from_mongo",
                        lambda: [{"clientId": "A", "preferredMedium": "email"},
                                 {"clientId": "B", "preferredMedium": "sms"}])
    monkeypatch.setattr(server, "_load_overrides",
                        lambda: {"include_only": [], "exclude": [], "clients": {}})

    def fake_do_request(method, path, payload, extra_headers, timeout):
        cid = payload["Request"]["payload"][0]["clientId"]
        if cid == "B":
            raise RuntimeError("connection reset")
        return {"status": 200, "ok": True, "latencyMs": 5, "body": {"Response": {"code": 200}}}

    monkeypatch.setattr(server, "_do_request", fake_do_request)
    out = server.fanout_test("/api/ai/v1/chatbot/predictions",
                             {"Request": {"payload": [{}]}})
    assert out["summary"] == {"total": 2, "ok": 1, "failed": 1}
    by_id = {r["clientId"]: r for r in out["results"]}
    assert by_id["A"]["ok"] is True
    assert by_id["B"]["ok"] is False
    assert "connection reset" in by_id["B"]["errorSnippet"]


def test_fanout_empty_clientlist_raises(monkeypatch):
    monkeypatch.setattr(server, "_fetch_clients_from_mongo", lambda: [])
    monkeypatch.setattr(server, "_load_overrides",
                        lambda: {"include_only": [], "exclude": [], "clients": {}})
    with pytest.raises(ValueError):
        server.fanout_test("/x", {"Request": {"payload": [{}]}})
