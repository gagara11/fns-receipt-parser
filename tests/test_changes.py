import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from fns_mcp.store import Store
from fns_mcp.client import SyncError
from fns_mcp.sync import SyncService


class ReceiptChangesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "receipts.sqlite3"
        self.store = Store(self.path)

    @staticmethod
    def receipt(key="one", total="12.34"):
        return {"key": key, "totalSum": total, "createdDate": "2026-10-01T12:00:00",
                "fiscalDocumentNumber": 12345678901234567890}

    @staticmethod
    def detail(amount=1234):
        return {"totalSum": amount, "cashTotalSum": 0, "ecashTotalSum": amount,
                "operationType": 1, "items": [{"price": amount, "quantity": 1, "sum": amount}]}

    def test_migrates_existing_rows_once_and_preserves_archive_id(self):
        legacy_path = Path(self.tmp.name) / "legacy.sqlite3"
        with sqlite3.connect(legacy_path) as db:
            db.executescript("""
                CREATE TABLE receipts (key TEXT PRIMARY KEY, purchased_at TEXT, seller TEXT,
                    raw TEXT NOT NULL, detail TEXT, error TEXT);
                CREATE TABLE state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            """)
            db.execute("INSERT INTO receipts VALUES (?,?,?,?,?,?)", (
                "old", "2026-10-01", "Shop", json.dumps(self.receipt("old")),
                json.dumps(self.detail()), None))

        migrated = Store(legacy_path)
        page = migrated.changes()
        self.assertEqual(len(page["archive_id"]), 36)
        self.assertEqual(page["receipts"][0]["seq"], 1)
        self.assertEqual(page["receipts"][0]["key"], "old")
        reopened = Store(legacy_path)
        self.assertEqual(reopened.changes()["archive_id"], page["archive_id"])
        self.assertEqual(reopened.get("old")["receipt"]["key"], "old")

    def test_put_assigns_stable_sequence_and_wire_decimal_strings(self):
        self.store.put(self.receipt(), self.detail())
        first = self.store.changes()
        self.store.put(self.receipt(), self.detail())
        repeated = self.store.changes()

        self.assertEqual(first["receipts"][0]["seq"], repeated["receipts"][0]["seq"])
        self.assertEqual(first["receipts"][0]["content_hash"], repeated["receipts"][0]["content_hash"])
        item = repeated["receipts"][0]
        self.assertEqual(item["receipt"]["totalSum"], "12.34")
        self.assertEqual(item["receipt"]["fiscalDocumentNumber"], "12345678901234567890")
        self.assertEqual(item["fiscal_data"]["totalSum"], "1234")
        self.assertEqual(item["fiscal_data"]["items"][0]["price"], "1234")
        self.assertEqual(item["fiscal_data"]["items"][0]["quantity"], 1)
        self.assertEqual(item["fiscal_data"]["operationType"], 1)

    def test_wire_keeps_decimal_lexemes_and_stringifies_all_amount_fields(self):
        self.store.put(self.receipt("precise"), self.detail())
        raw = ('{"key":"precise","totalSum":0.123456789012345678901234567890,'
               '"creditSum":11.2300,"provisionSum":2.50,"fiscalDocumentNumber":12345678901234567890}')
        detail = ('{"totalSum":0.123456789012345678901234567890,"creditSum":11.2300,'
                  '"provisionSum":2.50,"operationType":1,"items":[{"price":0.123456789012345678901234567890,'
                  '"quantity":1.25,"sum":0.123456789012345678901234567890}]}')
        with self.store.connect() as db:
            db.execute("UPDATE receipts SET raw=?,detail=? WHERE key='precise'", (raw, detail))

        item = self.store.changes()["receipts"][0]
        exact = "0.123456789012345678901234567890"
        self.assertEqual(item["receipt"]["totalSum"], exact)
        self.assertEqual(item["receipt"]["creditSum"], "11.2300")
        self.assertEqual(item["receipt"]["provisionSum"], "2.50")
        self.assertEqual(item["fiscal_data"]["totalSum"], exact)
        self.assertEqual(item["fiscal_data"]["items"][0]["price"], exact)
        self.assertEqual(item["fiscal_data"]["items"][0]["quantity"], 1.25)

    def test_error_retries_do_not_create_transient_or_duplicate_revisions(self):
        receipt = self.receipt()
        self.store.put(receipt, None)
        first = self.store.changes()["receipts"][0]
        self.store.put(receipt, None, "detail_unavailable")
        failed = self.store.changes()["receipts"][0]
        self.assertGreater(failed["seq"], first["seq"])
        self.assertEqual(first["content_hash"], failed["content_hash"])
        self.store.put(receipt, None)
        self.store.put(receipt, None, "detail_unavailable")
        repeated = self.store.changes()["receipts"][0]
        self.assertEqual(failed["seq"], repeated["seq"])
        self.assertEqual(repeated["error"], "detail_unavailable")

        self.store.put(receipt, self.detail())
        success = self.store.changes()["receipts"][0]
        self.assertGreater(success["seq"], repeated["seq"])
        self.assertIsNone(success["error"])

    def test_changed_old_row_moves_beyond_fixed_window_and_is_delivered_later(self):
        self.store.put(self.receipt("one"), None)
        self.store.put(self.receipt("two"), None)
        window = self.store.changes(limit=1)
        self.assertEqual(window["until_seq"], 2)
        self.assertEqual(window["next_after_seq"], 1)
        self.assertTrue(window["has_more"])

        self.store.put(self.receipt("one", "99.99"), None)
        last_in_window = self.store.changes(after_seq=1, until_seq=2)
        self.assertEqual([row["key"] for row in last_in_window["receipts"]], ["two"])
        self.assertEqual(last_in_window["next_after_seq"], 2)
        self.assertFalse(last_in_window["has_more"])

        next_pass = self.store.changes(after_seq=2)
        self.assertEqual([row["key"] for row in next_pass["receipts"]], ["one"])
        self.assertEqual(next_pass["receipts"][0]["seq"], 3)

    def test_sync_retry_of_same_failure_does_not_create_fake_revisions(self):
        client = Mock()
        receipt = self.receipt()
        client.receipts.return_value = [receipt]
        client.detail.side_effect = SyncError("detail_unavailable")
        service = SyncService(self.store, lambda: client)
        service.run_once()
        first = self.store.changes()["receipts"][0]
        service.run_once()
        repeated = self.store.changes()["receipts"][0]
        self.assertEqual(first["seq"], repeated["seq"])
        self.assertEqual(repeated["error"], "detail_unavailable")

    def test_sync_records_changed_list_data_for_already_detailed_receipt(self):
        self.store.put(self.receipt(), self.detail())
        before = self.store.changes()["receipts"][0]
        changed = self.receipt(total="56.78")
        client = Mock()
        client.receipts.return_value = [changed]
        service = SyncService(self.store, lambda: client)
        service.run_once()
        after = self.store.changes()["receipts"][0]
        client.detail.assert_not_called()
        self.assertGreater(after["seq"], before["seq"])
        self.assertEqual(after["receipt"]["totalSum"], "56.78")
        self.assertEqual(after["fiscal_data"]["totalSum"], "1234")


if __name__ == "__main__":
    unittest.main()
