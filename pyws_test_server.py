import copy
import json
import os
import re
import subprocess
from typing import Any, Dict, Iterator, List, Optional, Tuple

import requests
from mcp.server.fastmcp import FastMCP

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


if __name__ == "__main__":
    mcp.run()
