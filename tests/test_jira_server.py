import sys
import types
import unittest
from unittest.mock import Mock, patch


class _FakeFastMCP:
    def __init__(self, _name: str) -> None:
        self._name = _name

    def tool(self):
        def decorator(func):
            return func

        return decorator

    def run(self) -> None:
        return None


if "mcp.server.fastmcp" not in sys.modules:
    fake_fastmcp = types.ModuleType("mcp.server.fastmcp")
    fake_fastmcp.FastMCP = _FakeFastMCP
    sys.modules["mcp"] = types.ModuleType("mcp")
    sys.modules["mcp.server"] = types.ModuleType("mcp.server")
    sys.modules["mcp.server.fastmcp"] = fake_fastmcp

if "requests" not in sys.modules:
    fake_requests = types.ModuleType("requests")
    fake_requests.request = Mock()
    fake_requests.get = Mock()
    fake_requests.put = Mock()
    fake_requests.HTTPError = Exception
    fake_requests.RequestException = Exception
    sys.modules["requests"] = fake_requests

import jira_server


class JiraServerTests(unittest.TestCase):
    def test_jira_search_issues_rejects_empty_jql(self) -> None:
        with self.assertRaises(ValueError):
            jira_server.jira_search_issues("   ")

    def test_increase_original_estimate_updates_by_default_one_hour(self) -> None:
        with patch("jira_server._jira_get") as mock_get, patch("jira_server._jira_put") as mock_put:
            mock_get.side_effect = [
                {"issues": [{"id": "29020"}]},
                {
                    "fields": {
                        "summary": "Test issue",
                        "timeoriginalestimate": 7200,
                    }
                },
            ]

            result = jira_server.jira_increase_original_estimate("FIN-6605")

            self.assertEqual(result["issue_key"], "FIN-6605")
            self.assertEqual(result["previous_original_estimate_seconds"], 7200)
            self.assertEqual(result["increment_seconds"], 3600)
            self.assertEqual(result["updated_original_estimate_seconds"], 10800)
            mock_put.assert_called_once_with(
                "/rest/api/3/issue/29020",
                payload={"fields": {"timetracking": {"originalEstimate": "180m"}}},
            )

    def test_set_original_estimate_updates_to_explicit_value(self) -> None:
        with patch("jira_server._jira_get") as mock_get, patch("jira_server._jira_put") as mock_put:
            mock_get.side_effect = [
                {"issues": [{"id": "29020"}]},
                {"fields": {"summary": "Test issue", "timeoriginalestimate": 7200}},
                {"fields": {"timeoriginalestimate": 115200, "timetracking": {"originalEstimate": "4d"}}},
            ]

            result = jira_server.jira_set_original_estimate("FIN-6605", "4d")

            self.assertEqual(result["issue_key"], "FIN-6605")
            self.assertEqual(result["previous_original_estimate_seconds"], 7200)
            self.assertEqual(result["updated_original_estimate_seconds"], 115200)
            self.assertEqual(result["updated_original_estimate"], "4d")
            mock_put.assert_called_once_with(
                "/rest/api/3/issue/29020",
                payload={"fields": {"timetracking": {"originalEstimate": "4d"}}},
            )

    def test_increase_original_estimate_rejects_invalid_inputs(self) -> None:
        with self.assertRaises(ValueError):
            jira_server.jira_increase_original_estimate("  ", increment_hours=1)
        with self.assertRaises(ValueError):
            jira_server.jira_increase_original_estimate("FIN-1", increment_hours=0)

    def test_jira_put_uses_requests_request(self) -> None:
        with patch("jira_server.requests.request") as mock_request, patch(
            "jira_server._base_url", return_value="https://example.atlassian.net"
        ), patch("jira_server._headers", return_value={"Accept": "application/json"}), patch(
            "jira_server._auth", return_value=("email@example.com", "token")
        ):
            response = Mock()
            response.text = ""
            mock_request.return_value = response

            jira_server._jira_put("/rest/api/3/issue/FIN-1", payload={"fields": {"x": 1}})

            mock_request.assert_called_once()
            response.raise_for_status.assert_called_once()


if __name__ == "__main__":
    unittest.main()
