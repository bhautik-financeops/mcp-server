import os
import re
from typing import Any, Dict, List, Optional

import certifi
from bson import ObjectId
from bson.errors import InvalidId
from mcp.server.fastmcp import FastMCP
from pymongo import MongoClient
from pymongo.errors import PyMongoError

_OID_RE = re.compile(r"^[0-9a-fA-F]{24}$")

# ---------------------------------------------------------------------------
# Environment loading
# ---------------------------------------------------------------------------

def _load_env_file() -> None:
    env_file = os.getenv("MONGO_ENV_FILE", os.path.join(os.path.dirname(__file__), ".env"))
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

# ---------------------------------------------------------------------------
# Connection management
# ---------------------------------------------------------------------------

_CLIENTS: Dict[str, MongoClient] = {}

_ENV_ALIASES: Dict[str, str] = {
    "prod": "prod",
    "production": "prod",
    "qa": "qa",
    "stage": "qa",
    "staging": "qa",
    "non-prod": "qa",
}


def _resolve_env(env: str) -> str:
    normalized = env.strip().lower()
    resolved = _ENV_ALIASES.get(normalized)
    if resolved is None:
        raise ValueError(
            f"Unknown environment '{env}'. Valid values: prod, qa (aliases: production, stage, staging)."
        )
    return resolved


def _get_uri(env: str) -> str:
    resolved = _resolve_env(env)
    var_name = f"MONGO_URI_{resolved.upper()}"
    uri = os.getenv(var_name, "").strip()
    if not uri:
        raise ValueError(
            f"Missing required environment variable: {var_name}. "
            f"Add it to the .env file in the mcp-servers directory."
        )
    return uri


def _get_client(env: str) -> MongoClient:
    resolved = _resolve_env(env)
    if resolved not in _CLIENTS:
        uri = _get_uri(resolved)
        _CLIENTS[resolved] = MongoClient(uri, serverSelectionTimeoutMS=10_000, tlsCAFile=certifi.where())
    return _CLIENTS[resolved]


def _assert_writable(env: str) -> None:
    if _resolve_env(env) == "prod":
        raise ValueError("Write operations are not permitted on the prod environment.")


# ---------------------------------------------------------------------------
# BSON coercion
# ---------------------------------------------------------------------------

def _coerce_value(value: Any) -> Any:
    """
    Recursively convert Extended JSON notation and bare hex strings to native
    BSON types so that filters/documents passed as plain JSON work correctly.

    Conversions applied:
      - {"$oid": "<24-hex>"}  →  ObjectId("<24-hex>")   (Extended JSON v2)
      - Any 24-hex bare string used as a dict value     →  ObjectId (best-effort)
    """
    if isinstance(value, dict):
        if list(value.keys()) == ["$oid"]:
            raw = value["$oid"]
            try:
                return ObjectId(raw)
            except (InvalidId, TypeError):
                raise ValueError(f"Invalid ObjectId string: {raw!r}")
        return {k: _coerce_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_coerce_value(item) for item in value]
    if isinstance(value, str) and _OID_RE.match(value):
        try:
            return ObjectId(value)
        except InvalidId:
            pass
    return value


def _coerce_filter(filter_doc: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not filter_doc:
        return {}
    return {k: _coerce_value(v) for k, v in filter_doc.items()}


# ---------------------------------------------------------------------------
# MCP server
# ---------------------------------------------------------------------------

mcp = FastMCP("mongodb")


@mcp.tool()
def mongo_list_databases(env: str) -> Dict[str, Any]:
    """
    List all databases on the target MongoDB cluster.

    Args:
        env: Target environment — "prod" or "qa" (aliases: production, stage, staging).
    """
    try:
        client = _get_client(env)
        dbs = client.list_database_names()
        return {"env": _resolve_env(env), "databases": dbs, "count": len(dbs)}
    except PyMongoError as exc:
        raise ValueError(f"MongoDB error: {exc}") from exc


@mcp.tool()
def mongo_list_collections(env: str, database: str) -> Dict[str, Any]:
    """
    List all collections in a given database.

    Args:
        env: Target environment — "prod" or "qa".
        database: Database name.
    """
    if not database.strip():
        raise ValueError("database must not be empty")
    try:
        client = _get_client(env)
        collections = client[database.strip()].list_collection_names()
        return {
            "env": _resolve_env(env),
            "database": database.strip(),
            "collections": sorted(collections),
            "count": len(collections),
        }
    except PyMongoError as exc:
        raise ValueError(f"MongoDB error: {exc}") from exc


@mcp.tool()
def mongo_find(
    env: str,
    database: str,
    collection: str,
    filter: Optional[Dict[str, Any]] = None,
    projection: Optional[Dict[str, Any]] = None,
    limit: int = 10,
    sort_field: Optional[str] = None,
    sort_order: int = -1,
) -> Dict[str, Any]:
    """
    Query documents from a MongoDB collection.

    Args:
        env: Target environment — "prod" or "qa".
        database: Database name.
        collection: Collection name.
        filter: MongoDB filter document (default: {} — all documents).
        projection: Fields to include/exclude (e.g., {"_id": 0, "name": 1}).
        limit: Maximum number of documents to return (default: 10, max: 200).
        sort_field: Optional field name to sort by.
        sort_order: 1 for ascending, -1 for descending (default: -1).
    """
    if not database.strip():
        raise ValueError("database must not be empty")
    if not collection.strip():
        raise ValueError("collection must not be empty")

    safe_limit = max(1, min(limit, 200))
    query_filter = _coerce_filter(filter)

    try:
        client = _get_client(env)
        col = client[database.strip()][collection.strip()]
        cursor = col.find(query_filter, projection)

        if sort_field:
            cursor = cursor.sort(sort_field, sort_order)

        cursor = cursor.limit(safe_limit)
        docs = []
        for doc in cursor:
            if "_id" in doc:
                doc["_id"] = str(doc["_id"])
            docs.append(doc)

        return {
            "env": _resolve_env(env),
            "database": database.strip(),
            "collection": collection.strip(),
            "count": len(docs),
            "limit": safe_limit,
            "documents": docs,
        }
    except PyMongoError as exc:
        raise ValueError(f"MongoDB error: {exc}") from exc


@mcp.tool()
def mongo_find_one(
    env: str,
    database: str,
    collection: str,
    filter: Optional[Dict[str, Any]] = None,
    projection: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Fetch a single document matching a filter.

    Args:
        env: Target environment — "prod" or "qa".
        database: Database name.
        collection: Collection name.
        filter: MongoDB filter document.
        projection: Fields to include/exclude.
    """
    if not database.strip():
        raise ValueError("database must not be empty")
    if not collection.strip():
        raise ValueError("collection must not be empty")

    try:
        client = _get_client(env)
        doc = client[database.strip()][collection.strip()].find_one(_coerce_filter(filter), projection)
        if doc is None:
            return {"env": _resolve_env(env), "found": False, "document": None}
        if "_id" in doc:
            doc["_id"] = str(doc["_id"])
        return {"env": _resolve_env(env), "found": True, "document": doc}
    except PyMongoError as exc:
        raise ValueError(f"MongoDB error: {exc}") from exc


@mcp.tool()
def mongo_count(
    env: str,
    database: str,
    collection: str,
    filter: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Count documents matching a filter in a collection.

    Args:
        env: Target environment — "prod" or "qa".
        database: Database name.
        collection: Collection name.
        filter: MongoDB filter document (default: {} — counts all documents).
    """
    if not database.strip():
        raise ValueError("database must not be empty")
    if not collection.strip():
        raise ValueError("collection must not be empty")

    try:
        client = _get_client(env)
        col = client[database.strip()][collection.strip()]
        count = col.count_documents(_coerce_filter(filter))
        return {
            "env": _resolve_env(env),
            "database": database.strip(),
            "collection": collection.strip(),
            "count": count,
        }
    except PyMongoError as exc:
        raise ValueError(f"MongoDB error: {exc}") from exc


@mcp.tool()
def mongo_aggregate(
    env: str,
    database: str,
    collection: str,
    pipeline: List[Dict[str, Any]],
    limit: int = 100,
) -> Dict[str, Any]:
    """
    Run a MongoDB aggregation pipeline.

    Args:
        env: Target environment — "prod" or "qa".
        database: Database name.
        collection: Collection name.
        pipeline: List of aggregation stage documents (e.g., [{"$match": {...}}, {"$group": {...}}]).
        limit: Maximum number of result documents to return (default: 100, max: 500).
    """
    if not database.strip():
        raise ValueError("database must not be empty")
    if not collection.strip():
        raise ValueError("collection must not be empty")
    if not pipeline:
        raise ValueError("pipeline must not be empty")

    write_stages = {"$out", "$merge"}
    if _resolve_env(env) == "prod" and any(stage.keys() & write_stages for stage in pipeline):
        raise ValueError(
            "Aggregation pipelines containing $out or $merge are not permitted on the prod environment."
        )

    safe_limit = max(1, min(limit, 500))

    try:
        client = _get_client(env)
        col = client[database.strip()][collection.strip()]
        results = list(col.aggregate(pipeline))[:safe_limit]

        for doc in results:
            if "_id" in doc:
                doc["_id"] = str(doc["_id"])

        return {
            "env": _resolve_env(env),
            "database": database.strip(),
            "collection": collection.strip(),
            "count": len(results),
            "results": results,
        }
    except PyMongoError as exc:
        raise ValueError(f"MongoDB error: {exc}") from exc


@mcp.tool()
def mongo_distinct(
    env: str,
    database: str,
    collection: str,
    field: str,
    filter: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Get distinct values for a field in a collection.

    Args:
        env: Target environment — "prod" or "qa".
        database: Database name.
        collection: Collection name.
        field: Field name to get distinct values for.
        filter: Optional filter to restrict the documents considered.
    """
    if not database.strip():
        raise ValueError("database must not be empty")
    if not collection.strip():
        raise ValueError("collection must not be empty")
    if not field.strip():
        raise ValueError("field must not be empty")

    try:
        client = _get_client(env)
        col = client[database.strip()][collection.strip()]
        values = col.distinct(field.strip(), _coerce_filter(filter))
        serializable = [str(v) if not isinstance(v, (str, int, float, bool, type(None))) else v for v in values]
        return {
            "env": _resolve_env(env),
            "database": database.strip(),
            "collection": collection.strip(),
            "field": field.strip(),
            "distinct_count": len(serializable),
            "values": serializable,
        }
    except PyMongoError as exc:
        raise ValueError(f"MongoDB error: {exc}") from exc


@mcp.tool()
def mongo_get_indexes(env: str, database: str, collection: str) -> Dict[str, Any]:
    """
    List all indexes on a collection.

    Args:
        env: Target environment — "prod" or "qa".
        database: Database name.
        collection: Collection name.
    """
    if not database.strip():
        raise ValueError("database must not be empty")
    if not collection.strip():
        raise ValueError("collection must not be empty")

    try:
        client = _get_client(env)
        col = client[database.strip()][collection.strip()]
        indexes = list(col.index_information().values())
        return {
            "env": _resolve_env(env),
            "database": database.strip(),
            "collection": collection.strip(),
            "indexes": indexes,
            "count": len(indexes),
        }
    except PyMongoError as exc:
        raise ValueError(f"MongoDB error: {exc}") from exc


@mcp.tool()
def mongo_collection_stats(env: str, database: str, collection: str) -> Dict[str, Any]:
    """
    Get storage and document statistics for a collection.

    Args:
        env: Target environment — "prod" or "qa".
        database: Database name.
        collection: Collection name.
    """
    if not database.strip():
        raise ValueError("database must not be empty")
    if not collection.strip():
        raise ValueError("collection must not be empty")

    try:
        client = _get_client(env)
        db = client[database.strip()]
        stats = db.command("collStats", collection.strip())
        return {
            "env": _resolve_env(env),
            "database": database.strip(),
            "collection": collection.strip(),
            "document_count": stats.get("count"),
            "avg_document_size_bytes": stats.get("avgObjSize"),
            "total_size_bytes": stats.get("size"),
            "storage_size_bytes": stats.get("storageSize"),
            "index_count": stats.get("nindexes"),
            "total_index_size_bytes": stats.get("totalIndexSize"),
        }
    except PyMongoError as exc:
        raise ValueError(f"MongoDB error: {exc}") from exc


@mcp.tool()
def mongo_insert_one(
    env: str,
    database: str,
    collection: str,
    document: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Insert a single document into a collection. QA only — not permitted on prod.

    Args:
        env: Target environment — "qa" only (prod is read-only).
        database: Database name.
        collection: Collection name.
        document: Document to insert.
    """
    _assert_writable(env)
    if not database.strip():
        raise ValueError("database must not be empty")
    if not collection.strip():
        raise ValueError("collection must not be empty")
    if not document:
        raise ValueError("document must not be empty")

    try:
        client = _get_client(env)
        result = client[database.strip()][collection.strip()].insert_one(document)
        return {
            "env": _resolve_env(env),
            "database": database.strip(),
            "collection": collection.strip(),
            "inserted_id": str(result.inserted_id),
        }
    except PyMongoError as exc:
        raise ValueError(f"MongoDB error: {exc}") from exc


@mcp.tool()
def mongo_insert_many(
    env: str,
    database: str,
    collection: str,
    documents: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """
    Insert multiple documents into a collection. QA only — not permitted on prod.

    Args:
        env: Target environment — "qa" only (prod is read-only).
        database: Database name.
        collection: Collection name.
        documents: List of documents to insert.
    """
    _assert_writable(env)
    if not database.strip():
        raise ValueError("database must not be empty")
    if not collection.strip():
        raise ValueError("collection must not be empty")
    if not documents:
        raise ValueError("documents list must not be empty")

    try:
        client = _get_client(env)
        result = client[database.strip()][collection.strip()].insert_many(documents)
        return {
            "env": _resolve_env(env),
            "database": database.strip(),
            "collection": collection.strip(),
            "inserted_count": len(result.inserted_ids),
            "inserted_ids": [str(oid) for oid in result.inserted_ids],
        }
    except PyMongoError as exc:
        raise ValueError(f"MongoDB error: {exc}") from exc


@mcp.tool()
def mongo_update_one(
    env: str,
    database: str,
    collection: str,
    filter: Dict[str, Any],
    update: Dict[str, Any],
    upsert: bool = False,
) -> Dict[str, Any]:
    """
    Update the first document matching a filter. QA only — not permitted on prod.

    Args:
        env: Target environment — "qa" only (prod is read-only).
        database: Database name.
        collection: Collection name.
        filter: Query filter to match the target document.
        update: Update document (must use update operators, e.g. {"$set": {...}}).
        upsert: If True, insert the document if no match is found (default: False).
    """
    _assert_writable(env)
    if not database.strip():
        raise ValueError("database must not be empty")
    if not collection.strip():
        raise ValueError("collection must not be empty")
    if not filter:
        raise ValueError("filter must not be empty — refusing full-collection update")
    if not update:
        raise ValueError("update must not be empty")

    try:
        client = _get_client(env)
        result = client[database.strip()][collection.strip()].update_one(_coerce_filter(filter), update, upsert=upsert)
        return {
            "env": _resolve_env(env),
            "database": database.strip(),
            "collection": collection.strip(),
            "matched_count": result.matched_count,
            "modified_count": result.modified_count,
            "upserted_id": str(result.upserted_id) if result.upserted_id else None,
        }
    except PyMongoError as exc:
        raise ValueError(f"MongoDB error: {exc}") from exc


@mcp.tool()
def mongo_update_many(
    env: str,
    database: str,
    collection: str,
    filter: Dict[str, Any],
    update: Dict[str, Any],
    upsert: bool = False,
) -> Dict[str, Any]:
    """
    Update all documents matching a filter. QA only — not permitted on prod.

    Args:
        env: Target environment — "qa" only (prod is read-only).
        database: Database name.
        collection: Collection name.
        filter: Query filter to match target documents.
        update: Update document (must use update operators, e.g. {"$set": {...}}).
        upsert: If True, insert a document if no match is found (default: False).
    """
    _assert_writable(env)
    if not database.strip():
        raise ValueError("database must not be empty")
    if not collection.strip():
        raise ValueError("collection must not be empty")
    if not filter:
        raise ValueError("filter must not be empty — refusing full-collection update")
    if not update:
        raise ValueError("update must not be empty")

    try:
        client = _get_client(env)
        result = client[database.strip()][collection.strip()].update_many(_coerce_filter(filter), update, upsert=upsert)
        return {
            "env": _resolve_env(env),
            "database": database.strip(),
            "collection": collection.strip(),
            "matched_count": result.matched_count,
            "modified_count": result.modified_count,
            "upserted_id": str(result.upserted_id) if result.upserted_id else None,
        }
    except PyMongoError as exc:
        raise ValueError(f"MongoDB error: {exc}") from exc


@mcp.tool()
def mongo_delete_one(
    env: str,
    database: str,
    collection: str,
    filter: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Delete the first document matching a filter. QA only — not permitted on prod.

    Args:
        env: Target environment — "qa" only (prod is read-only).
        database: Database name.
        collection: Collection name.
        filter: Query filter to match the target document.
    """
    _assert_writable(env)
    if not database.strip():
        raise ValueError("database must not be empty")
    if not collection.strip():
        raise ValueError("collection must not be empty")
    if not filter:
        raise ValueError("filter must not be empty — refusing full-collection delete")

    try:
        client = _get_client(env)
        result = client[database.strip()][collection.strip()].delete_one(_coerce_filter(filter))
        return {
            "env": _resolve_env(env),
            "database": database.strip(),
            "collection": collection.strip(),
            "deleted_count": result.deleted_count,
        }
    except PyMongoError as exc:
        raise ValueError(f"MongoDB error: {exc}") from exc


@mcp.tool()
def mongo_delete_many(
    env: str,
    database: str,
    collection: str,
    filter: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Delete all documents matching a filter. QA only — not permitted on prod.

    Args:
        env: Target environment — "qa" only (prod is read-only).
        database: Database name.
        collection: Collection name.
        filter: Query filter to match target documents.
    """
    _assert_writable(env)
    if not database.strip():
        raise ValueError("database must not be empty")
    if not collection.strip():
        raise ValueError("collection must not be empty")
    if not filter:
        raise ValueError("filter must not be empty — refusing full-collection delete")

    try:
        client = _get_client(env)
        result = client[database.strip()][collection.strip()].delete_many(_coerce_filter(filter))
        return {
            "env": _resolve_env(env),
            "database": database.strip(),
            "collection": collection.strip(),
            "deleted_count": result.deleted_count,
        }
    except PyMongoError as exc:
        raise ValueError(f"MongoDB error: {exc}") from exc


if __name__ == "__main__":
    mcp.run()
