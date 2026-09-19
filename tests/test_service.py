import asyncio
import csv
import io
import json
import tempfile
import time
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import Mock, patch

import export_nalog_receipts as upstream
from fns_mcp.client import FNSClient, SyncError, atomic_json
from fns_mcp.configure import save_session
from fns_mcp.store import Store
from fns_mcp.sync import INTERVAL, SyncService
from tests.test_api import FakeResponse


class HardenedTransportTest(unittest.TestCase):
    def test_redirect_does_not_forward_authorization(self):
        req = upstream.urllib.request.Request(upstream.LIST_URL, headers={"Authorization": "Bearer private"})
        self.assertIsNone(upstream.NoRedirect().redirect_request(req, None, 302, "", {}, "https://attacker.invalid"))

    def test_oversized_response_is_rejected(self):
        with patch.object(upstream, "MAX_RESPONSE_BYTES", 10), patch.object(upstream._opener, "open", return_value=FakeResponse(b"x"*11)):
            with self.assertRaises(upstream.ApiError):
                upstream.request_json_once(upstream.LIST_URL, "test", {})

    def test_csv_formula_injection_is_escaped(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)/"items.csv"
            upstream.write_csv(path, ["name", "sum"], [{"name": " =HYPERLINK(1)", "sum": -10}])
            with path.open(encoding="utf-8-sig") as handle:
                row = list(csv.DictReader(handle, delimiter=";"))[0]
            self.assertEqual(row["name"], "' =HYPERLINK(1)")
            self.assertEqual(row["sum"], "-10")


class ServiceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = Store(self.root/"db.sqlite3")

    @staticmethod
    def receipt(key="one"):
        return {"key": key, "totalSum": "120", "kktOwner": "Test shop", "createdDate": "2026-09-01T12:00:00"}

    @staticmethod
    def detail(paid=12000, op=1):
        return {"dateTime": "2026-09-01T12:00:00", "user": "Test shop", "totalSum": 12000,
                "cashTotalSum": 0, "ecashTotalSum": paid, "operationType": op,
                "items": [{"name": "Test item", "price": 12000, "quantity": 1, "sum": 12000}]}

    def test_archive_is_idempotent_and_survives_reopen(self):
        for _ in range(2):
            self.store.put(self.receipt(), self.detail())
        reopened = Store(self.root/"db.sqlite3")
        self.assertEqual(reopened.status()["receipts"], 1)
        self.assertEqual(reopened.get("one")["fiscal_data"]["items"][0]["name"], "Test item")

    def test_error_does_not_overwrite_existing_detail(self):
        self.store.put(self.receipt(), self.detail())
        self.store.put(self.receipt(), None, "fns_unavailable")
        self.assertTrue(self.store.has_detail("one"))

    def test_sql_injection_is_literal(self):
        self.store.put(self.receipt(), self.detail())
        self.assertEqual(self.store.list(seller="' OR 1=1 --")["total"], 0)
        self.assertFalse(self.store.get("' OR 1=1 --")["found"])

    def test_filters_and_limits(self):
        self.store.put(self.receipt(), self.detail())
        self.assertEqual(self.store.list(date_from="2026-09-02")["total"], 0)
        self.assertEqual(self.store.list(date_to="2026-09-01")["total"], 1)
        for args in ({"limit": 201}, {"offset": -1}, {"date_from": "2026-02-30"},
                     {"date_from": "2026-09-02", "date_to": "2026-09-01"}):
            with self.assertRaises(ValueError):
                self.store.list(**args)

    def test_returns_and_prepaid_settlement_are_not_double_counted(self):
        self.store.put(self.receipt("advance"), self.detail(12000))
        settlement = self.detail(0)
        settlement["prepaidSum"] = 12000
        self.store.put(self.receipt("settlement"), settlement)
        self.store.put(self.receipt("refund"), self.detail(2000, 2))
        self.store.put(self.receipt("unknown"), self.detail(100, 99))
        report = self.store.spending(100)
        self.assertEqual(report["net"], "100")
        self.assertEqual(report["excluded_receipts"], 1)
        self.assertEqual(self.store.spending(1)["net"], "10000")

    def test_sync_downloads_only_missing_and_retries_failed_details(self):
        client = Mock()
        client.receipts.return_value = [self.receipt("one"), self.receipt("two")]
        client.detail.side_effect = [self.detail(), SyncError("fns_unavailable")]
        svc = SyncService(self.store, lambda: client)
        with patch("fns_mcp.sync.time.time", return_value=1000):
            svc.run_once()
        self.assertEqual(self.store.status()["last_error"], "partial_failure")
        self.assertEqual(self.store.status()["next_sync_at"], 1000 + 14400)
        client.detail.reset_mock(side_effect=True)
        client.detail.return_value = self.detail()
        svc.run_once()
        client.detail.assert_called_once_with("two")
        self.assertEqual(self.store.status()["detailed_receipts"], 2)
        self.assertIsNone(self.store.status()["last_error"])

    def test_missing_session_is_explicit_not_empty_success(self):
        svc = SyncService(self.store, lambda: FNSClient(self.root/"missing.json"))
        svc.run_once()
        self.assertEqual(self.store.status()["last_error"], "authentication_required")
        self.assertNotIn("last_success_at", self.store.status())

    def test_private_error_text_not_saved(self):
        client = Mock()
        client.load.side_effect = RuntimeError("secret-token-phone")
        with self.assertLogs("fns_mcp.sync", level="ERROR") as logs:
            SyncService(self.store, lambda: client).run_once()
        self.assertNotIn("secret-token-phone", json.dumps(self.store.status()) + str(logs.output))

    def test_concurrent_sync_rejected(self):
        svc = SyncService(self.store, Mock())
        svc.lock.acquire()
        try:
            self.assertFalse(svc.trigger()["accepted"])
            svc.run_once()
            svc.client_factory.assert_not_called()
        finally:
            svc.lock.release()

    def test_refresh_rotates_and_persists_token(self):
        path = self.root/"session.json"
        atomic_json(path, {"refreshToken": "old-refresh", "sourceDeviceId": "test-device"})
        client = FNSClient(path, delay=0)
        client._post = Mock(side_effect=[{"token": "new-token", "refreshToken": "new-refresh"}, {"receipts": []}])
        self.assertEqual(client.request("/v1/receipt", {}), {"receipts": []})
        self.assertEqual(json.loads(path.read_text())["refreshToken"], "new-refresh")
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_session_setup_normalizes_bearer_and_requires_refresh(self):
        path = self.root/"session.json"
        save_session(path, "Bearer sample", "refresh", "device")
        self.assertEqual(json.loads(path.read_text())["token"], "sample")
        for token, refresh, device in [("sample", "", "device"), ("x\ny", "refresh", "device")]:
            with self.assertRaises(ValueError):
                save_session(path, token, refresh, device)
        self.assertEqual(json.loads(path.read_text())["token"], "sample")

    def test_401_refreshes_once(self):
        client = FNSClient(self.root/"session.json", delay=0)
        client.session = {"token": "expired", "refreshToken": "refresh", "sourceDeviceId": "device"}
        client._post = Mock(side_effect=[upstream.ApiError("secret", status=401),
                                       {"token": "new"}, {"items": []}])
        self.assertEqual(client.request("/v1/receipt/fiscal_data", {}), {"items": []})
        self.assertEqual(client._post.call_count, 3)

    def test_invalid_refresh_does_not_replace_good_session(self):
        path = self.root/"session.json"
        atomic_json(path, {"refreshToken": "old", "sourceDeviceId": "test"})
        client = FNSClient(path, delay=0)
        client._post = Mock(return_value={"error": "bad"})
        with self.assertRaises(SyncError):
            client.request("/v1/receipt", {})
        self.assertEqual(json.loads(path.read_text())["refreshToken"], "old")

    def test_pagination_stall_is_not_success(self):
        client = FNSClient(self.root/"unused")
        client.request = Mock(return_value={"receipts": [self.receipt(str(i)) for i in range(10)], "hasMore": True})
        with self.assertRaisesRegex(SyncError, "pagination_stalled"):
            list(client.receipts())

    def test_short_page_with_has_more_is_followed(self):
        client = FNSClient(self.root/"unused")
        client.request = Mock(side_effect=[{"receipts": [self.receipt()], "hasMore": True},
                                          {"receipts": [], "hasMore": False}])
        self.assertEqual(len(list(client.receipts())), 1)
        self.assertEqual(client.request.call_count, 2)


class SchedulerTest(unittest.IsolatedAsyncioTestCase):
    async def test_future_schedule_survives_restart_and_manual_trigger_wakes_it(self):
        with tempfile.TemporaryDirectory() as root:
            store = Store(Path(root)/"db")
            store.state(next_sync_at=time.time()+INTERVAL)
            client = Mock()
            client.receipts.return_value = []
            svc = SyncService(store, lambda: client)
            task = asyncio.create_task(svc.schedule())
            try:
                await asyncio.sleep(.02)
                client.load.assert_not_called()
                svc.trigger()
                for _ in range(100):
                    if store.status().get("last_success_at"):
                        break
                    await asyncio.sleep(.01)
                self.assertIsNotNone(store.status().get("last_success_at"))
            finally:
                svc.stop.set()
                svc.wake.set()
                await asyncio.wait_for(task, timeout=2)
