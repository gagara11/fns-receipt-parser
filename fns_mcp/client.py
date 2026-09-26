"""FNS session handling. No credentials or upstream error bodies are logged."""

import json
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import export_nalog_receipts as upstream
from fns_mcp.redaction import clean_text, receipt_ref


class SyncError(Exception):
    """A safe, fixed error code, suitable for status and logs."""

    def __init__(self, code, *, context=None, retryable=False):
        super().__init__(code)
        self.context = context or {}
        self.retryable = retryable


def atomic_json(path: Path, data: dict):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(prefix=".session-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


class FNSClient:
    def __init__(self, session_file: Path, delay: float = 3.2):
        self.session_file = session_file
        self.delay = delay
        self.last_request = 0.0
        self.session = None
        self.observe = lambda event, **fields: None

    @staticmethod
    def auth_error(error):
        return error.status == 401 or (error.status in (403, 422)
                                      and error.service_code == "authentication.failed")

    def error_context(self, error, path, payload, attempt):
        secrets = list((self.session or {}).values()) + [payload.get("key")]
        return {"operation": {"/v1/receipt": "receipt_list", "/v1/receipt/fiscal_data": "receipt_detail",
                              "/v1/auth/token": "token_refresh"}[path],
                "http_status": error.status, "fns_code": clean_text(error.service_code, secrets, 96),
                "message": clean_text(error.service_message, secrets), "error_type": error.kind,
                "attempt": attempt, "receipt_ref": receipt_ref(payload.get("key")),
                "offset": payload.get("offset")}

    def load(self):
        try:
            if self.session_file.stat().st_size > 65536:
                raise SyncError("invalid_session_file")
            data = json.loads(self.session_file.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise SyncError("authentication_required") from None
        except (ValueError, OSError):
            raise SyncError("invalid_session_file") from None
        if not isinstance(data, dict) or not (data.get("token") or data.get("refreshToken")):
            raise SyncError("authentication_required")
        self.session = data

    def _post(self, path, token, payload):
        # Paths are code constants; MCP clients cannot supply URLs or headers.
        if path not in ("/v1/receipt", "/v1/receipt/fiscal_data", "/v1/auth/token"):
            raise ValueError("Unsupported FNS operation")
        for attempt in range(3):
            time.sleep(max(0, self.delay - (time.monotonic() - self.last_request)))
            self.last_request = time.monotonic()
            operation = {"/v1/receipt": "receipt_list", "/v1/receipt/fiscal_data": "receipt_detail",
                         "/v1/auth/token": "token_refresh"}[path]
            info = {"operation": operation, "attempt": attempt + 1,
                    "receipt_ref": receipt_ref(payload.get("key")), "offset": payload.get("offset")}
            self.observe("request_started", **info)
            started = time.monotonic()
            try:
                data = upstream.request_json_once(upstream.BASE + path, token, payload)
            except upstream.ApiError as error:
                context = self.error_context(error, path, payload, attempt + 1)
                context["duration_ms"] = round((time.monotonic()-started)*1000, 2)
                error.context = context
                self.observe("request_failed", **context)
                # 422 is overloaded by FNS. Retry unknown rejections, not authentication.
                retryable = error.retryable or (error.status == 422 and not self.auth_error(error)
                                                and path != "/v1/auth/token")
                error.retryable = retryable
                if not retryable or attempt == 2:
                    raise
                delay = min(30, max(2 ** attempt, error.retry_after or 0))
                self.observe("request_retry", **info, retry_in_seconds=delay)
                time.sleep(delay)
            else:
                self.observe("request_succeeded", **info, http_status=200,
                             duration_ms=round((time.monotonic()-started)*1000, 2))
                return data

    def _refresh(self):
        refresh = self.session.get("refreshToken")
        device = self.session.get("sourceDeviceId")
        if not refresh or not device:
            raise SyncError("authentication_required")
        try:
            data = self._post("/v1/auth/token", "", {
                "refreshToken": refresh,
                "deviceInfo": {"sourceDeviceId": device, "sourceType": "WEB",
                               "appVersion": "1.0.0", "metaDetails": {
                                   "userAgent": self.session.get("userAgent", "Mozilla/5.0")}},
            })
        except upstream.ApiError as error:
            context = getattr(error, "context", {})
            if error.status in (400, 401, 403) or self.auth_error(error):
                raise SyncError("authentication_required", context=context) from None
            raise SyncError("refresh_temporarily_failed", context=context, retryable=True) from None
        if not isinstance(data, dict) or not isinstance(data.get("token"), str) or not data["token"]:
            raise SyncError("invalid_refresh_response")
        for field in ("token", "refreshToken", "tokenExpireIn", "refreshTokenExpiresIn"):
            if data.get(field):
                self.session[field] = data[field]
        # Rotated refresh tokens survive container recreation and VPS restart.
        atomic_json(self.session_file, self.session)
        self.observe("token_refreshed", operation="token_refresh")

    def _token(self):
        if self.session is None:
            self.load()
        expiry = self.session.get("tokenExpireIn")
        expired = not self.session.get("token")
        if expiry:
            try:
                dt = datetime.fromisoformat(expiry.replace("Z", "+00:00"))
                expired |= dt.astimezone(timezone.utc).timestamp() <= time.time() + 300
            except (ValueError, TypeError, AttributeError):
                expired = True
        if expired:
            self._refresh()
        return self.session["token"]

    def request(self, path, payload):
        for attempt in range(2):
            try:
                data = self._post(path, self._token(), payload)
                if not isinstance(data, dict):
                    raise SyncError("invalid_fns_response")
                return data
            except upstream.ApiError as error:
                if self.auth_error(error) and attempt == 0:
                    self._refresh()
                    continue
                code = "authentication_required" if self.auth_error(error) else {
                    403: "access_denied", 422: "fns_rejected_request", 429: "rate_limited"}.get(
                        error.status, "fns_unavailable")
                raise SyncError(code, context=getattr(error, "context", {}), retryable=error.retryable) from None

    def receipts(self):
        seen = set()
        offset = 0
        for _ in range(10000):
            data = self.request("/v1/receipt", upstream.build_list_payload(10, offset))
            page = data.get("receipts")
            if not isinstance(page, list):
                raise SyncError("invalid_receipts_page")
            self.observe("page_received", operation="receipt_list", offset=offset, page_size=len(page))
            new = 0
            for receipt in page:
                if not isinstance(receipt, dict) or not isinstance(receipt.get("key"), str):
                    raise SyncError("invalid_receipt")
                key = receipt["key"]
                if not key or len(key) > 1024:
                    raise SyncError("invalid_receipt_key")
                if key not in seen:
                    seen.add(key)
                    new += 1
                    yield receipt
            if data.get("hasMore") is False or not page or (len(page) < 10 and not data.get("hasMore")):
                return
            if new == 0:
                raise SyncError("pagination_stalled")
            offset += len(page)
        raise SyncError("pagination_limit")

    def detail(self, key):
        data = self.request("/v1/receipt/fiscal_data", {"key": key})
        if not isinstance(data.get("items"), list) or "totalSum" not in data:
            raise SyncError("invalid_fiscal_data")
        return data
