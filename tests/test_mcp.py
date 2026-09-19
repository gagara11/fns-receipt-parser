import tempfile
import unittest
from pathlib import Path

from starlette.testclient import TestClient
from fns_mcp.server import create_app


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
        self.assertEqual({t["name"] for t in tools}, {"sync_status", "list_receipts", "get_receipt", "spending_summary", "sync_now"})

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
