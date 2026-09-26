import tempfile
import time
import unittest
from pathlib import Path

from starlette.testclient import TestClient
from fns_mcp.server import create_app
from fns_mcp.store import Store


class MCPTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        path = Path(self.tmp.name)
        (path/"mcp_token").write_text("x"*48)
        self.client = TestClient(create_app(path, path, scheduler=False), base_url="http://localhost:8000")
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        self.headers = {"Authorization": "Bearer " + "x"*48,
                        "Accept": "application/json, text/event-stream", "MCP-Protocol-Version": "2025-06-18"}

    def call(self, method, params=None, headers=None):
        return self.client.post("/mcp", headers=headers or self.headers,
                                json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}})

    def test_health_reveals_no_account_data(self):
        self.assertEqual(self.client.get("/healthz").json(), {"status": "ok"})

    def test_unauthorized_clients_cannot_read_or_sync(self):
        self.assertEqual(self.client.post("/mcp", json={}).status_code, 401)
        self.assertEqual(self.client.post("/mcp", headers={"Authorization": "Bearer wrong"}, json={}).status_code, 401)

    def test_initialize_and_tools_list(self):
        response = self.call("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                          "clientInfo": {"name": "test", "version": "1"}})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["result"]["serverInfo"]["name"], "fns-receipts")
        tools = self.call("tools/list").json()["result"]["tools"]
        self.assertEqual({t["name"] for t in tools}, {"sync_status", "list_receipts", "get_receipt", "spending_summary", "sync_now", "sync_history", "sync_events"})

    def test_real_tool_call(self):
        response = self.call("tools/call", {"name": "sync_status", "arguments": {}}).json()
        self.assertEqual(response["result"]["structuredContent"]["interval_seconds"], 14400)
        self.assertNotIn("x"*48, str(response))

    def test_host_and_browser_origin_are_rejected(self):
        self.assertEqual(self.call("tools/list", headers={**self.headers, "Host": "evil.invalid"}).status_code, 421)
        self.assertEqual(self.call("tools/list", headers={**self.headers, "Origin": "https://evil.invalid"}).status_code, 403)

    def test_body_limit_is_enforced(self):
        response = self.client.post("/mcp", headers={**self.headers, "Content-Type": "application/json"},
                                    content=b"x"*65537)
        self.assertEqual(response.status_code, 413)

    def test_diagnostics_tools_return_bounded_history(self):
        for name, field in [('sync_history', 'runs'), ('sync_events', 'events')]:
            response = self.call('tools/call', {'name':name, 'arguments':{'limit':5}}).json()['result']
            self.assertFalse(response.get('isError', False), response)
            self.assertEqual(response['structuredContent'][field], [])

    def test_metrics_require_auth_and_do_not_expose_identifiers(self):
        self.assertEqual(self.client.get('/metrics').status_code, 401)
        response = self.client.get('/metrics', headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertIn('fns_receipts 0', response.text)
        self.assertIn('fns_sync_running 0', response.text)
        self.assertIn('fns_sync_stale 1', response.text)
        self.assertNotIn('x'*48, response.text)
        self.assertNotIn('seller', response.text)


class SchedulerLifetimeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)
        (self.path / "mcp_token").write_text("x" * 48)
        self.store = Store(self.path / "receipts.sqlite3")
        self.headers = {"Authorization": "Bearer " + "x" * 48,
                        "Accept": "application/json, text/event-stream"}

    def wait_for_attempt(self, after=0):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            status = self.store.status()
            if status.get("last_attempt_at", 0) > after and not status.get("running"):
                return status
            time.sleep(.01)
        self.fail("Background sync did not execute independently of the MCP request lifetime")

    def test_due_sync_runs_without_any_mcp_client(self):
        self.store.state(next_sync_at=time.time() + .05)
        with TestClient(create_app(self.path, self.path), base_url="http://localhost:8000"):
            status = self.wait_for_attempt()
            self.assertEqual(status["last_error"], "authentication_required")
            self.assertAlmostEqual(status["next_sync_at"] - status["last_attempt_at"], 14400)

    def test_manual_sync_still_runs_after_stateless_request_closes(self):
        with TestClient(create_app(self.path, self.path), base_url="http://localhost:8000") as client:
            client.post("/mcp", headers=self.headers, json={"jsonrpc": "2.0", "id": 1,
                        "method": "initialize", "params": {"protocolVersion": "2025-06-18",
                        "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}})
            first = self.wait_for_attempt()["last_attempt_at"]
            response = client.post("/mcp", headers=self.headers, json={"jsonrpc": "2.0", "id": 2,
                        "method": "tools/call", "params": {"name": "sync_now", "arguments": {}}})
            self.assertTrue(response.json()["result"]["structuredContent"]["accepted"])
            self.assertEqual(self.wait_for_attempt(first)["last_error"], "authentication_required")
