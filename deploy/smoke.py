"""Run inside the container; tests the real MCP transport without exposing secrets."""
import json
import urllib.error
import urllib.request
from pathlib import Path

base = "http://127.0.0.1:8000"
assert json.load(urllib.request.urlopen(base+"/healthz", timeout=5)) == {"status": "ok"}
try:
    urllib.request.urlopen(urllib.request.Request(base+"/mcp", data=b"{}"), timeout=5)
    raise AssertionError("Unauthenticated request accepted")
except urllib.error.HTTPError as error:
    assert error.code == 401
token = Path("/secrets/mcp_token").read_text().strip()
def rpc(method, params):
    req = urllib.request.Request(base+"/mcp", data=json.dumps({"jsonrpc":"2.0","id":1,"method":method,"params":params}).encode(),
        headers={"Authorization":"Bearer "+token,"Accept":"application/json, text/event-stream",
                 "Content-Type":"application/json", "MCP-Protocol-Version":"2025-06-18"})
    result = json.load(urllib.request.urlopen(req,timeout=10))
    assert "error" not in result
    return result["result"]
assert rpc("initialize", {"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"deployment-check","version":"1"}})["serverInfo"]["name"] == "fns-receipts"
assert len(rpc("tools/list", {})["tools"]) == 5
status = rpc("tools/call", {"name":"sync_status","arguments":{}})["structuredContent"]
assert status["interval_seconds"] == 14400
print("MCP smoke OK; sync state:", status.get("last_error") or "ready")
