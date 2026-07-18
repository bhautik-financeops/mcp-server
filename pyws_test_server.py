import copy
import json
import os
import re
import subprocess
from typing import Any, Dict, Iterator, List, Optional, Tuple

import certifi
import requests
import yaml
from mcp.server.fastmcp import FastMCP
from pymongo import MongoClient

_HERE = os.path.dirname(os.path.abspath(__file__))
_CONTEXT_DIR = os.path.join(_HERE, "pyws_context")

API_KEY_HEADER = "X-py-webservice-api-key"
_CONTAINERS = {"sync": "pythonWebServerSync", "async": "pythonWebServerAsync"}
_COMPOSE_FILE = "build_release/docker-compose-files/app/docker-compose.local.mac.yml"
_STARTUP_SCRIPT = "build_release/scripts/app/python-ws-startup.sh"
_HTTP_METHODS = ("get", "post", "put", "patch", "delete")


def _load_env_file() -> None:
    env_file = os.getenv("PYWS_ENV_FILE", os.path.join(_HERE, ".env"))
    if not os.path.exists(env_file):
        return
    with open(env_file, "r", encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


_load_env_file()


def _required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ValueError(f"Missing required environment variable: {name}")
    return value


def _base_url() -> str:
    return _required_env("PYWS_BASE_URL").rstrip("/")


def _repo_dir() -> str:
    directory = _required_env("PYWS_REPO_DIR")
    if not os.path.isdir(directory):
        raise ValueError(f"PYWS_REPO_DIR is not a directory: {directory}")
    return directory


_OPENAPI_CACHE: Optional[Dict[str, Any]] = None


def _load_openapi() -> Dict[str, Any]:
    global _OPENAPI_CACHE
    if _OPENAPI_CACHE is None:
        path = os.path.join(_CONTEXT_DIR, "openapi.json")
        if not os.path.exists(path):
            raise ValueError(f"Missing OpenAPI spec: {path}")
        with open(path, "r", encoding="utf-8") as handle:
            _OPENAPI_CACHE = json.load(handle)
    return _OPENAPI_CACHE


def _iter_operations(spec: Dict[str, Any]) -> Iterator[Tuple[str, str, Dict[str, Any]]]:
    for path, ops in spec.get("paths", {}).items():
        if not isinstance(ops, dict):
            continue
        for method, op in ops.items():
            if method.lower() in _HTTP_METHODS:
                yield path, method.upper(), op


def _list_endpoints(spec: Dict[str, Any], filter_str: str = "") -> List[Dict[str, str]]:
    needle = filter_str.strip().lower()
    rows: List[Dict[str, str]] = []
    for path, method, _op in _iter_operations(spec):
        if needle and needle not in path.lower():
            continue
        rows.append({"path": path, "method": method})
    return sorted(rows, key=lambda r: (r["path"], r["method"]))


def _request_body(op: Dict[str, Any]) -> Dict[str, Any]:
    content = op.get("requestBody", {}).get("content", {})
    body = content.get("application/json", {})
    return {"example": body.get("example"), "schema": body.get("schema")}


def _describe_endpoint(spec: Dict[str, Any], path: str) -> Dict[str, Any]:
    ops = spec.get("paths", {}).get(path)
    if not isinstance(ops, dict):
        raise ValueError(f"Unknown endpoint path: {path}")
    operations: List[Dict[str, Any]] = []
    for method, op in ops.items():
        if method.lower() not in _HTTP_METHODS:
            continue
        operations.append(
            {
                "method": method.upper(),
                "security": op.get("security"),
                "parameters": [
                    {"name": p.get("name"), "in": p.get("in")}
                    for p in op.get("parameters", [])
                ],
                "requestBody": _request_body(op),
            }
        )
    return {"path": path, "operations": operations}


mcp = FastMCP("pyws-test")


@mcp.tool()
def list_endpoints(filter: str = "") -> Dict[str, Any]:
    """List py_webservice endpoints (path + method), optionally substring-filtered by path."""
    rows = _list_endpoints(_load_openapi(), filter)
    return {"count": len(rows), "endpoints": rows}


@mcp.tool()
def describe_endpoint(path: str) -> Dict[str, Any]:
    """Describe one endpoint: methods, security, params, and request-body example/schema."""
    return _describe_endpoint(_load_openapi(), path)


def _do_request(
    method: str,
    path: str,
    payload: Optional[Dict[str, Any]] = None,
    extra_headers: Optional[Dict[str, str]] = None,
    timeout: int = 60,
) -> Dict[str, Any]:
    url = f"{_base_url()}{path}"
    headers = {"Content-Type": "application/json", API_KEY_HEADER: _required_env("PYWS_API_KEY")}
    if extra_headers:
        headers.update(extra_headers)
    import time as _time
    started = _time.monotonic()
    try:
        response = requests.request(method.upper(), url, headers=headers, json=payload, timeout=timeout)
    except requests.exceptions.RequestException as exc:
        return {
            "status": None,
            "ok": False,
            "latencyMs": int((_time.monotonic() - started) * 1000),
            "body": str(exc),
            "error": str(exc),
        }
    latency_ms = int((_time.monotonic() - started) * 1000)
    try:
        body: Any = response.json()
    except ValueError:
        body = response.text
    return {
        "status": response.status_code,
        "ok": 200 <= response.status_code < 300,
        "latencyMs": latency_ms,
        "body": body,
    }


@mcp.tool()
def call_endpoint(
    path: str,
    payload: Optional[Dict[str, Any]] = None,
    method: str = "POST",
    extra_headers: Optional[Dict[str, str]] = None,
    timeout: int = 60,
) -> Dict[str, Any]:
    """Fire one authenticated request at the running py_webservice and return status/body/latency."""
    return _do_request(method, path, payload, extra_headers, timeout)


@mcp.tool()
def health() -> Dict[str, Any]:
    """Check app reachability: GET /ping plus authenticated GET /dev/platform-configurations."""
    result: Dict[str, Any] = {}
    try:
        result["ping"] = _do_request("GET", "/ping", None, None, 10)
    except Exception as exc:  # noqa: BLE001 - report unreachable rather than crash
        result["ping"] = {"ok": False, "error": str(exc)}
    try:
        result["platform"] = _do_request("GET", "/dev/platform-configurations", None, None, 10)
    except Exception as exc:  # noqa: BLE001
        result["platform"] = {"ok": False, "error": str(exc)}
    return result


def _load_overrides() -> Dict[str, Any]:
    path = os.path.join(_CONTEXT_DIR, "client_overrides.yaml")
    if not os.path.exists(path):
        return {"include_only": [], "exclude": [], "clients": {}}
    with open(path, "r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    data.setdefault("include_only", [])
    data.setdefault("exclude", [])
    data.setdefault("clients", {})
    return data


def _fetch_clients_from_mongo() -> List[Dict[str, Any]]:
    uri = _required_env("MONGO_URI_QA")
    db_name = os.getenv("PYWS_DB", "master").strip()
    collection = os.getenv("PYWS_CLIENTS_COLLECTION", "clients").strip()
    id_field = os.getenv("PYWS_CLIENT_ID_FIELD", "_id").strip()
    medium_field = os.getenv("PYWS_MEDIUM_FIELD", "communicationMedium").strip()
    client = MongoClient(uri, serverSelectionTimeoutMS=10_000, tlsCAFile=certifi.where())
    try:
        cursor = client[db_name][collection].find({}, {id_field: 1, medium_field: 1})
        clients: List[Dict[str, Any]] = []
        for doc in cursor:
            raw_id = doc.get(id_field)
            if raw_id is None:
                continue
            clients.append({"clientId": str(raw_id), "preferredMedium": doc.get(medium_field)})
    finally:
        client.close()
    return clients


def _merge_clients(mongo_clients: List[Dict[str, Any]], overrides: Dict[str, Any]) -> List[Dict[str, Any]]:
    include_only = {str(c) for c in overrides.get("include_only", [])}
    exclude = {str(c) for c in overrides.get("exclude", [])}
    per_client = overrides.get("clients", {}) or {}

    merged: Dict[str, Dict[str, Any]] = {}
    for entry in mongo_clients:
        cid = str(entry["clientId"])
        if cid in exclude:
            continue
        if include_only and cid not in include_only:
            continue
        merged[cid] = {
            "clientId": cid,
            "preferredMedium": entry.get("preferredMedium"),
            "sampleCustomerId": None,
            "source": "mongo",
        }

    for cid, forcing in per_client.items():
        cid = str(cid)
        if cid in exclude:
            continue
        if include_only and cid not in include_only:
            continue
        record = merged.get(cid) or {"clientId": cid, "preferredMedium": None,
                                     "sampleCustomerId": None, "source": "override"}
        if forcing.get("preferredMedium") is not None:
            record["preferredMedium"] = forcing["preferredMedium"]
        if forcing.get("sampleCustomerId") is not None:
            record["sampleCustomerId"] = forcing["sampleCustomerId"]
        merged[cid] = record

    return sorted(merged.values(), key=lambda r: r["clientId"])


@mcp.tool()
def get_clients(filter: Optional[str] = None) -> Dict[str, Any]:
    """List distinct clients (clientId + preferred medium) from Mongo, merged with local overrides."""
    merged = _merge_clients(_fetch_clients_from_mongo(), _load_overrides())
    if filter:
        needle = filter.strip().lower()
        merged = [c for c in merged if needle in c["clientId"].lower()]
    return {"count": len(merged), "clients": merged}


def _build_client_payload(
    base_payload: Dict[str, Any], client: Dict[str, Any], medium_field: str
) -> Dict[str, Any]:
    body = copy.deepcopy(base_payload)
    request = body.get("Request") if isinstance(body, dict) else None
    items = request.get("payload") if isinstance(request, dict) else None
    targets = items if isinstance(items, list) and items else [body]
    for target in targets:
        target["clientId"] = client["clientId"]
        if client.get("sampleCustomerId"):
            target["customerId"] = client["sampleCustomerId"]
        if client.get("preferredMedium"):
            target[medium_field] = client["preferredMedium"]
    return body


@mcp.tool()
def fanout_test(
    path: str,
    base_payload: Dict[str, Any],
    client_filter: Optional[str] = None,
    medium_field: str = "communicationMedium",
    timeout: int = 60,
) -> Dict[str, Any]:
    """Fire `path` once per distinct client, each via its preferred medium; return a per-client aggregate."""
    clients = _merge_clients(_fetch_clients_from_mongo(), _load_overrides())
    if client_filter:
        needle = client_filter.strip().lower()
        clients = [c for c in clients if needle in c["clientId"].lower()]
    if not clients:
        raise ValueError("No clients to fan out over (empty client list). Check Mongo/overrides/filter.")

    results: List[Dict[str, Any]] = []
    ok_count = 0
    for client in clients:
        try:
            payload = _build_client_payload(base_payload, client, medium_field)
            resp = _do_request("POST", path, payload, None, timeout)
            ok = bool(resp["ok"])
            snippet = None if ok else str(resp["body"])[:500]
            results.append({
                "clientId": client["clientId"], "medium": client.get("preferredMedium"),
                "status": resp["status"], "ok": ok, "errorSnippet": snippet, "body": resp["body"],
            })
        except Exception as exc:  # noqa: BLE001 - isolate one client's failure
            results.append({
                "clientId": client["clientId"], "medium": client.get("preferredMedium"),
                "status": None, "ok": False, "errorSnippet": str(exc)[:500], "body": None,
            })
        if results[-1]["ok"]:
            ok_count += 1
    return {
        "summary": {"total": len(results), "ok": ok_count, "failed": len(results) - ok_count},
        "results": results,
    }


_KNOWN_CONTAINERS = set(_CONTAINERS.values()) | {"redisMasterPythonWs"}


def _engine_container(engine: str) -> str:
    key = engine.strip().lower()
    if key not in _CONTAINERS:
        raise ValueError(f"Unknown engine '{engine}'. Use 'sync' or 'async'.")
    return _CONTAINERS[key]


def _docker_names_from_ps(ps_stdout: str) -> List[Dict[str, str]]:
    rows: List[Dict[str, str]] = []
    for line in ps_stdout.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        name, state, status = parts[0].strip(), parts[1].strip(), parts[2].strip()
        if name in _KNOWN_CONTAINERS:
            rows.append({"name": name, "state": state, "status": status})
    return rows


@mcp.tool()
def stack_up(rebuild: bool = True) -> Dict[str, Any]:
    """Start the local py_webservice stack via python-ws-startup.sh (detached; poll with stack_status)."""
    repo = _repo_dir()
    proc = subprocess.Popen(
        ["sh", _STARTUP_SCRIPT],
        cwd=repo,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    return {"started": True, "pid": proc.pid, "pollWith": "stack_status",
            "note": "Build + compose up runs in the background; give it a few minutes."}


@mcp.tool()
def stack_status() -> Dict[str, Any]:
    """Report docker state for pythonWebServerSync / pythonWebServerAsync / redisMasterPythonWs."""
    try:
        done = subprocess.run(
            ["docker", "ps", "-a", "--format", "{{.Names}}\t{{.State}}\t{{.Status}}"],
            capture_output=True, text=True, timeout=20,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "error": f"docker command failed: {exc}"}
    if done.returncode != 0:
        return {"ok": False, "error": done.stderr.strip() or "docker ps failed"}
    return {"ok": True, "containers": _docker_names_from_ps(done.stdout)}


@mcp.tool()
def container_logs(engine: str = "sync", lines: int = 200, grep: Optional[str] = None) -> Dict[str, Any]:
    """Tail docker logs for the sync API or async worker, optionally regex-filtered (default ERROR/Traceback)."""
    name = _engine_container(engine)
    try:
        done = subprocess.run(
            ["docker", "logs", "--tail", str(lines), name],
            capture_output=True, text=True, timeout=30,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return {"engine": engine, "container": name, "ok": False, "error": f"docker command failed: {exc}"}
    if done.returncode != 0:
        return {"engine": engine, "container": name, "ok": False,
                "error": done.stderr.strip() or "container not found / stack down"}
    pattern = grep.strip() if (grep and grep.strip()) else r"ERROR|Traceback|Exception"
    try:
        regex = re.compile(pattern)
    except re.error as exc:
        return {"engine": engine, "container": name, "ok": False, "error": f"invalid grep regex: {exc}"}
    combined = (done.stdout + done.stderr).splitlines()
    matched = [ln for ln in combined if regex.search(ln)]
    return {"engine": engine, "container": name, "ok": True,
            "lineCount": len(combined), "matched": matched}


@mcp.tool()
def stack_restart(engine: str = "all") -> Dict[str, Any]:
    """Restart the sync container, the async container, or both (engine='all')."""
    targets = list(_CONTAINERS.values()) if engine.strip().lower() == "all" else [_engine_container(engine)]
    results = {}
    for name in targets:
        try:
            done = subprocess.run(["docker", "restart", name], capture_output=True, text=True, timeout=60)
            results[name] = {"ok": done.returncode == 0, "error": done.stderr.strip() or None}
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            results[name] = {"ok": False, "error": f"docker command failed: {exc}"}
    return {"restarted": results}


@mcp.tool()
def stack_down() -> Dict[str, Any]:
    """Stop the local stack via docker compose down."""
    try:
        done = subprocess.run(
            ["docker", "compose", "-f", _COMPOSE_FILE, "down"],
            cwd=_repo_dir(), capture_output=True, text=True, timeout=120,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "error": f"docker command failed: {exc}"}
    return {"ok": done.returncode == 0, "output": (done.stdout + done.stderr).strip()}


if __name__ == "__main__":
    mcp.run()
