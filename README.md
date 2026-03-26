# Jira MCP Server

This folder contains a lightweight MCP server for Jira Cloud.

## What it provides

- `jira_my_open_issues(max_results=20)`  
  Returns open issues assigned to the authenticated user in `JIRA_PROJECT_KEY`.
- `jira_search_issues(jql, max_results=20)`  
  Executes any JQL query and returns concise issue details.
- `jira_get_issue(issue_key)`  
  Fetches full details for a specific issue.
- `jira_set_original_estimate(issue_key, estimate)`  
  Sets original estimate explicitly (for example: `4d`, `8h`, `90m`).
- `jira_increase_original_estimate(issue_key, increment_hours=1.0)`  
  Increases original estimate by N hours.

## Setup

1. Create a virtual environment (recommended).
2. Install dependencies:

```bash
pip install -r mcp_servers/requirements-jira-mcp.txt
```

3. Set environment variables in a `.env` file in this folder:

- `JIRA_BASE_URL` (example: `https://financeops.atlassian.net`)
- `JIRA_EMAIL`
- `JIRA_API_TOKEN` (Atlassian API token)
- `JIRA_PROJECT_KEY` (example: `FIN`)

The server auto-loads `.env` by default. You can override with `JIRA_ENV_FILE`.

## Run locally

```bash
python mcp_servers/jira_server.py
```

## Cursor MCP config example

Add this to your Cursor MCP config once, so it works from any Cursor window:

```json
{
  "mcpServers": {
    "jira": {
      "command": "/Users/bhautikpithadiya/FinanceOps/macOS/bin/python",
      "args": ["/Users/bhautikpithadiya/FinanceOps/mcp-servers/jira_server.py"],
      "env": {
        "JIRA_ENV_FILE": "/Users/bhautikpithadiya/FinanceOps/mcp-servers/.env"
      }
    }
  }
}
```

## Notes

- This server is designed for Jira Cloud REST API v3.
- Keep API tokens out of source control.
- Jira search uses the current `/rest/api/3/search/jql` endpoint.
