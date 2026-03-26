# MCP Servers

This folder contains lightweight MCP servers for Jira Cloud and MongoDB.

---

## .env Configuration

Both servers load a `.env` file from the `mcp-servers` directory automatically. Create a single `.env` file here with all the variables below:

```dotenv
# ─── Jira ────────────────────────────────────────────────────────────────────
# Base URL of your Jira Cloud instance (no trailing slash)
JIRA_BASE_URL=https://your-org.atlassian.net

# Atlassian account email used for authentication
JIRA_EMAIL=you@example.com

# Atlassian API token — generate at https://id.atlassian.com/manage-profile/security/api-tokens
JIRA_API_TOKEN=your_atlassian_api_token

# Jira project key to scope queries (e.g. FIN, OPS)
JIRA_PROJECT_KEY=FIN

# ─── MongoDB ─────────────────────────────────────────────────────────────────
# Full MongoDB connection URI for the production cluster (read-only)
MONGO_URI_PROD=

# Full MongoDB connection URI for the QA / staging cluster (read + write)
MONGO_URI_QA=
```

> **Never commit `.env` to source control.** It is listed in `.gitignore`.

### Optional overrides

| Variable | Default | Purpose |
|---|---|---|
| `JIRA_ENV_FILE` | `.env` | Override path to the Jira env file |
| `MONGO_ENV_FILE` | `.env` (same dir as `mongodb_server.py`) | Override path to the MongoDB env file |

---

## Jira MCP Server

### What it provides

- `jira_my_open_issues(max_results=20)` — open issues assigned to the authenticated user in `JIRA_PROJECT_KEY`
- `jira_search_issues(jql, max_results=20)` — run any JQL query
- `jira_get_issue(issue_key)` — full details for a specific issue
- `jira_set_original_estimate(issue_key, estimate)` — set estimate explicitly (e.g. `4d`, `8h`, `90m`)
- `jira_increase_original_estimate(issue_key, increment_hours=1.0)` — increase estimate by N hours
- `jira_create_issue(summary, issue_type, ...)` — create a Story, Task, Bug, etc.
- `jira_create_subtask(parent_key, summary, ...)` — create a Sub-task under an existing issue
- `jira_transition_issue(issue_key, status_name)` — move an issue to a new status
- `jira_assign_issue(issue_key, assignee)` — change the assignee

### Setup

```bash
pip install -r requirements-jira-mcp.txt
```

### Run locally

```bash
python jira_server.py
```

---

## MongoDB MCP Server

### What it provides

Read operations (prod + qa):
- `mongo_list_databases(env)` — list all databases
- `mongo_list_collections(env, database)` — list all collections
- `mongo_find(env, database, collection, ...)` — query documents (max 200)
- `mongo_find_one(env, database, collection, ...)` — fetch a single document
- `mongo_count(env, database, collection, ...)` — count matching documents
- `mongo_aggregate(env, database, collection, pipeline, ...)` — run an aggregation pipeline
- `mongo_distinct(env, database, collection, field, ...)` — get distinct field values
- `mongo_get_indexes(env, database, collection)` — list indexes
- `mongo_collection_stats(env, database, collection)` — storage and document stats

Write operations (qa only — blocked on prod):
- `mongo_insert_one` / `mongo_insert_many`
- `mongo_update_one` / `mongo_update_many`
- `mongo_delete_one` / `mongo_delete_many`

The `env` parameter accepts `prod`, `production`, `qa`, `stage`, or `staging`.

### Setup

```bash
pip install -r requirements-mongodb-mcp.txt
```

### Run locally

```bash
python mongodb_server.py
```

---

## Cursor MCP config example

Add this to your Cursor MCP config to use both servers from any Cursor window:

```json
{
  "mcpServers": {
    "jira": {
      "command": "/path/to/your/venv/bin/python",
      "args": ["/path/to/mcp-servers/jira_server.py"],
      "env": {
        "JIRA_ENV_FILE": "/path/to/mcp-servers/.env"
      }
    },
    "mongodb": {
      "command": "/path/to/your/venv/bin/python",
      "args": ["/path/to/mcp-servers/mongodb_server.py"],
      "env": {
        "MONGO_ENV_FILE": "/path/to/mcp-servers/.env"
      }
    }
  }
}
```

---

## Notes

- Jira server targets Jira Cloud REST API v3.
- MongoDB write operations are intentionally blocked on `prod` to prevent accidental data mutations.
- Keep API tokens and connection URIs out of source control.
