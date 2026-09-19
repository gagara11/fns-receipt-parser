"""Interactive administrator-only credential setup, never exposed as an MCP tool."""
import getpass
import json
import os
import urllib.request
from pathlib import Path

from fns_mcp.client import atomic_json


def save_session(path, token, refresh, device):
    token = token.removeprefix("Bearer ").strip()
    values = {"token": token, "refreshToken": refresh.strip(), "sourceDeviceId": device.strip()}
    if any(not isinstance(v, str) or not v or len(v) > 16384 or "\n" in v or "\r" in v
           for v in values.values()):
        raise ValueError("All three session fields must be nonempty single-line strings")
    atomic_json(path, values)


def main():
    os.umask(0o077)
    secret_dir = Path(os.environ.get("FNS_SECRET_DIR", "/secrets"))
    print("Credentials stay on this VPS. Input is hidden; do not paste them into chat.")
    token = getpass.getpass("FNS token (Bearer prefix allowed): ")
    refresh = getpass.getpass("FNS refreshToken: ")
    device = getpass.getpass("FNS sourceDeviceId: ")
    try:
        save_session(secret_dir / "session.json", token, refresh, device)
    except ValueError as error:
        raise SystemExit(str(error)) from None
    request = urllib.request.Request("http://127.0.0.1:8000/mcp",
        data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                         "params": {"name": "sync_now", "arguments": {}}}).encode(),
        headers={"Authorization": "Bearer " + (secret_dir/"mcp_token").read_text().strip(),
                 "Content-Type": "application/json", "Accept": "application/json, text/event-stream",
                 "MCP-Protocol-Version": "2025-06-18"})
    try:
        result = json.load(urllib.request.urlopen(request, timeout=10))
        if result.get("result", {}).get("structuredContent", {}).get("accepted"):
            print("Session saved with mode 0600. Sync queued; check sync_status for its result.")
        else:
            print("Session saved. Sync is already active or must be requested through sync_now.")
    except Exception:
        print("Session saved. Server unavailable; request sync_now when it is ready.")


if __name__ == "__main__":
    main()
