import os
from typing import Any, Dict, Optional

import requests
from mcp.server.fastmcp import FastMCP


def _load_env_file() -> None:
    env_file = os.getenv("JIRA_ENV_FILE", ".env")
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
    url = _required_env("JIRA_BASE_URL").rstrip("/")
    if not url.startswith("http://") and not url.startswith("https://"):
        raise ValueError("JIRA_BASE_URL must start with http:// or https://")
    return url


def _auth() -> tuple[str, str]:
    return (_required_env("JIRA_EMAIL"), _required_env("JIRA_API_TOKEN"))


def _headers() -> Dict[str, str]:
    return {"Accept": "application/json"}


def _jira_get(path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return _jira_request("GET", path, params=params)


def _jira_request(
    method: str,
    path: str,
    *,
    params: Optional[Dict[str, Any]] = None,
    payload: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    url = f"{_base_url()}{path}"
    try:
        response = requests.request(
            method,
            url,
            headers=_headers(),
            auth=_auth(),
            params=params,
            json=payload,
            timeout=30,
        )
        response.raise_for_status()
    except requests.HTTPError as exc:
        body = ""
        if exc.response is not None:
            body = (exc.response.text or "").strip()
        raise ValueError(f"Jira API error ({method} {path}): {exc} {body}".strip()) from exc
    except requests.RequestException as exc:
        raise ValueError(f"Jira request failed ({method} {path}): {exc}") from exc

    if not response.text:
        return {}
    return response.json()


def _jira_put(path: str, payload: Dict[str, Any]) -> None:
    _jira_request("PUT", path, payload=payload)


def _resolve_issue_identifier(issue_key: str) -> str:
    data = _jira_get(
        "/rest/api/3/search/jql",
        params={"jql": f'issuekey = "{issue_key}"', "maxResults": 1, "fields": "summary"},
    )
    issues = data.get("issues", [])
    if not issues:
        raise ValueError(f"Issue not found: {issue_key}")
    return str(issues[0].get("id") or issue_key)


def _seconds_to_estimate_string(total_seconds: int) -> str:
    total_minutes = max(1, int(round(total_seconds / 60)))
    return f"{total_minutes}m"


def _validate_estimate_string(estimate: str) -> str:
    normalized = estimate.strip()
    if not normalized:
        raise ValueError("estimate must not be empty")
    return normalized


def _summarize_issue(issue: Dict[str, Any]) -> Dict[str, Any]:
    fields = issue.get("fields", {})
    assignee = fields.get("assignee") or {}
    status = fields.get("status") or {}
    priority = fields.get("priority") or {}
    return {
        "key": issue.get("key"),
        "summary": fields.get("summary"),
        "status": status.get("name"),
        "priority": priority.get("name"),
        "assignee": assignee.get("displayName"),
        "updated": fields.get("updated"),
    }


mcp = FastMCP("jira")


@mcp.tool()
def jira_my_open_issues(max_results: int = 20) -> Dict[str, Any]:
    """
    Fetch open issues assigned to the authenticated Jira user in the configured project.
    """
    project_key = _required_env("JIRA_PROJECT_KEY")
    jql = (
        f'project = "{project_key}" '
        "AND assignee = currentUser() "
        "AND statusCategory != Done "
        "ORDER BY updated DESC"
    )

    data = _jira_get(
        "/rest/api/3/search/jql",
        params={
            "jql": jql,
            "maxResults": max(1, min(max_results, 100)),
            "fields": "summary,status,assignee,priority,updated",
        },
    )

    issues = [_summarize_issue(issue) for issue in data.get("issues", [])]
    return {"total": data.get("total", len(issues)), "issues": issues}


@mcp.tool()
def jira_search_issues(jql: str, max_results: int = 20) -> Dict[str, Any]:
    """
    Run a Jira JQL search and return concise issue details.
    """
    if not jql.strip():
        raise ValueError("jql must not be empty")

    data = _jira_get(
        "/rest/api/3/search/jql",
        params={
            "jql": jql,
            "maxResults": max(1, min(max_results, 100)),
            "fields": "summary,status,assignee,priority,updated",
        },
    )
    issues = [_summarize_issue(issue) for issue in data.get("issues", [])]
    return {"total": data.get("total", len(issues)), "issues": issues}


@mcp.tool()
def jira_get_issue(issue_key: str) -> Dict[str, Any]:
    """
    Get full details for a single Jira issue key (for example: FIN-123).
    """
    if not issue_key.strip():
        raise ValueError("issue_key must not be empty")

    issue = _jira_get(
        f"/rest/api/3/issue/{issue_key.strip()}",
        params={
            "fields": (
                "summary,status,assignee,priority,updated,description,comment,"
                "timetracking,timeoriginalestimate,timeestimate"
            )
        },
    )
    return issue


@mcp.tool()
def jira_set_original_estimate(issue_key: str, estimate: str) -> Dict[str, Any]:
    """
    Set an issue's original estimate to a Jira time string (for example: 4d, 8h, 90m).
    """
    normalized_issue_key = issue_key.strip()
    if not normalized_issue_key:
        raise ValueError("issue_key must not be empty")
    normalized_estimate = _validate_estimate_string(estimate)

    issue_id_or_key = _resolve_issue_identifier(normalized_issue_key)
    issue = _jira_get(
        f"/rest/api/3/issue/{issue_id_or_key}",
        params={"fields": "timetracking,timeoriginalestimate,summary"},
    )
    fields = issue.get("fields", {})
    previous_seconds = fields.get("timeoriginalestimate") or 0

    _jira_put(
        f"/rest/api/3/issue/{issue_id_or_key}",
        payload={"fields": {"timetracking": {"originalEstimate": normalized_estimate}}},
    )

    refreshed = _jira_get(
        f"/rest/api/3/issue/{issue_id_or_key}",
        params={"fields": "timetracking,timeoriginalestimate"},
    )
    refreshed_fields = refreshed.get("fields", {})
    refreshed_timetracking = refreshed_fields.get("timetracking") or {}

    return {
        "issue_key": normalized_issue_key,
        "summary": fields.get("summary"),
        "previous_original_estimate_seconds": previous_seconds,
        "updated_original_estimate_seconds": refreshed_fields.get("timeoriginalestimate"),
        "updated_original_estimate": refreshed_timetracking.get("originalEstimate"),
    }


@mcp.tool()
def jira_increase_original_estimate(issue_key: str, increment_hours: float = 1.0) -> Dict[str, Any]:
    """
    Increase an issue's original estimate by N hours (default 1 hour).
    """
    normalized_issue_key = issue_key.strip()
    if not normalized_issue_key:
        raise ValueError("issue_key must not be empty")
    if increment_hours <= 0:
        raise ValueError("increment_hours must be greater than 0")

    issue_id_or_key = _resolve_issue_identifier(normalized_issue_key)
    issue = _jira_get(
        f"/rest/api/3/issue/{issue_id_or_key}",
        params={"fields": "timetracking,timeoriginalestimate,summary"},
    )
    fields = issue.get("fields", {})
    current_seconds = fields.get("timeoriginalestimate") or 0
    increment_seconds = int(round(increment_hours * 3600))
    updated_seconds = current_seconds + increment_seconds

    _jira_put(
        f"/rest/api/3/issue/{issue_id_or_key}",
        payload={
            "fields": {
                "timetracking": {
                    "originalEstimate": _seconds_to_estimate_string(updated_seconds),
                }
            }
        },
    )

    return {
        "issue_key": normalized_issue_key,
        "summary": fields.get("summary"),
        "previous_original_estimate_seconds": current_seconds,
        "increment_seconds": increment_seconds,
        "updated_original_estimate_seconds": updated_seconds,
    }


@mcp.tool()
def jira_create_issue(
    summary: str,
    issue_type: str = "Story",
    description: str = "",
    priority: str = "",
    assignee_account_id: str = "",
    parent_key: str = "",
) -> Dict[str, Any]:
    """
    Create a new Jira issue (Story, Task, Bug, etc.) in the configured project.
    Returns the created issue key and URL.

    Args:
        summary: Issue title / summary (required).
        issue_type: One of Story, Task, Bug, Epic, Sub-task (default: Story).
        description: Plain-text description of the issue.
        priority: Optional priority name — Highest, High, Medium, Low, Lowest.
        assignee_account_id: Optional Jira account ID to assign the issue to.
        parent_key: Optional parent issue key (required when issue_type is Sub-task).
    """
    if not summary.strip():
        raise ValueError("summary must not be empty")

    project_key = _required_env("JIRA_PROJECT_KEY")

    fields: Dict[str, Any] = {
        "project": {"key": project_key},
        "summary": summary.strip(),
        "issuetype": {"name": issue_type.strip()},
    }

    if description.strip():
        fields["description"] = {
            "type": "doc",
            "version": 1,
            "content": [
                {
                    "type": "paragraph",
                    "content": [{"type": "text", "text": description.strip()}],
                }
            ],
        }

    if priority.strip():
        fields["priority"] = {"name": priority.strip()}

    if assignee_account_id.strip():
        fields["assignee"] = {"accountId": assignee_account_id.strip()}

    if parent_key.strip():
        fields["parent"] = {"key": parent_key.strip()}

    result = _jira_request("POST", "/rest/api/3/issue", payload={"fields": fields})

    issue_key = result.get("key", "")
    base = _base_url()
    return {
        "key": issue_key,
        "id": result.get("id"),
        "url": f"{base}/browse/{issue_key}",
        "summary": summary.strip(),
        "issue_type": issue_type.strip(),
    }


@mcp.tool()
def jira_create_subtask(
    parent_key: str,
    summary: str,
    description: str = "",
    priority: str = "",
    assignee_account_id: str = "",
) -> Dict[str, Any]:
    """
    Create a Sub-task under an existing Jira issue.

    Args:
        parent_key: The parent issue key (for example: FIN-123).
        summary: Sub-task title / summary (required).
        description: Plain-text description of the sub-task.
        priority: Optional priority name — Highest, High, Medium, Low, Lowest.
        assignee_account_id: Optional Jira account ID to assign the sub-task to.
    """
    if not parent_key.strip():
        raise ValueError("parent_key must not be empty")
    if not summary.strip():
        raise ValueError("summary must not be empty")

    return jira_create_issue(
        summary=summary,
        issue_type="Sub-task",
        description=description,
        priority=priority,
        assignee_account_id=assignee_account_id,
        parent_key=parent_key,
    )


@mcp.tool()
def jira_transition_issue(issue_key: str, status_name: str) -> Dict[str, Any]:
    """
    Transition a Jira issue to a new status (for example: In Progress, Done, To Do).

    Args:
        issue_key: The issue key (for example: FIN-123).
        status_name: Target status name (case-insensitive match against available transitions).
    """
    if not issue_key.strip():
        raise ValueError("issue_key must not be empty")
    if not status_name.strip():
        raise ValueError("status_name must not be empty")

    normalized_key = issue_key.strip()
    target = status_name.strip().lower()

    transitions_data = _jira_get(f"/rest/api/3/issue/{normalized_key}/transitions")
    transitions = transitions_data.get("transitions", [])

    matched = next(
        (t for t in transitions if t.get("name", "").lower() == target),
        None,
    )
    if matched is None:
        available = [t.get("name") for t in transitions]
        raise ValueError(
            f"No transition named '{status_name}' found for {normalized_key}. "
            f"Available: {available}"
        )

    _jira_request(
        "POST",
        f"/rest/api/3/issue/{normalized_key}/transitions",
        payload={"transition": {"id": matched["id"]}},
    )

    return {
        "issue_key": normalized_key,
        "transitioned_to": matched.get("name"),
        "transition_id": matched.get("id"),
    }


@mcp.tool()
def jira_assign_issue(issue_key: str, assignee: str) -> Dict[str, Any]:
    """
    Change the assignee of a Jira issue.

    Args:
        issue_key: The issue key (for example: FIN-123).
        assignee: The person to assign the issue to. Accepts either:
                  - A Jira account ID (exact match, e.g. "712020:abc123...")
                  - A display name or email to search for (e.g. "Bhautik Pithadiya")
    """
    if not issue_key.strip():
        raise ValueError("issue_key must not be empty")
    if not assignee.strip():
        raise ValueError("assignee must not be empty")

    normalized_key = issue_key.strip()
    assignee_value = assignee.strip()

    # Resolve display name / email to an account ID if needed.
    # Jira account IDs follow the pattern "712020:uuid" — if it doesn't look like
    # one, treat it as a search query.
    account_id = assignee_value
    if ":" not in assignee_value:
        users = _jira_get(
            "/rest/api/3/user/search",
            params={"query": assignee_value, "maxResults": 10},
        )
        if not users:
            raise ValueError(
                f"No Jira user found matching '{assignee_value}'. "
                "Try providing the exact Jira account ID instead."
            )
        if len(users) > 1:
            matches = [
                f"{u.get('displayName')} <{u.get('emailAddress')}> — {u.get('accountId')}"
                for u in users
            ]
            raise ValueError(
                f"Multiple users match '{assignee_value}'. "
                f"Provide a more specific name/email or use the account ID directly:\n"
                + "\n".join(matches)
            )
        account_id = users[0]["accountId"]
        resolved_display_name = users[0].get("displayName", assignee_value)
    else:
        # Verify the account ID exists and fetch display name for the response.
        user_data = _jira_get(
            "/rest/api/3/user",
            params={"accountId": account_id},
        )
        resolved_display_name = user_data.get("displayName", account_id)

    _jira_request(
        "PUT",
        f"/rest/api/3/issue/{normalized_key}/assignee",
        payload={"accountId": account_id},
    )

    return {
        "issue_key": normalized_key,
        "assignee_account_id": account_id,
        "assignee_display_name": resolved_display_name,
        "message": f"Successfully assigned {normalized_key} to {resolved_display_name}.",
    }


if __name__ == "__main__":
    mcp.run()
