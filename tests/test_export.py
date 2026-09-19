import csv
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

import export_nalog_receipts as exporter

# Synthetic data only: no real receipts, names or INNs.
RECEIPT = {
    "key": "test-key-1",
    "createdDate": "2026-09-10T12:00:00",
    "receiveDate": "2026-09-10T12:01:00",
    "kktOwner": "ООО Тестовый продавец",
    "kktOwnerInn": "0000000000",
    "totalSum": 15050,
    "fiscalDocumentNumber": 1,
    "fiscalDriveNumber": "0000000000000001",
    "sourceCode": "TEST",
    "receiptState": "OK",
    "brandId": 7,
}
FISCAL_DATA = {
    "dateTime": "2026-09-10T11:59:00",
    "user": "ООО «Тест; кавычки»",
    "userInn": "1111111111",
    "totalSum": 15050,
    "items": [
        {"name": "Молоко", "price": 10025, "quantity": 1, "sum": 10025, "nds": 2,
         "ndsSum": 911, "paymentType": 4, "productType": 1},
        {"name": "Хлеб", "price": 5025, "quantity": 1, "sum": 5025},
    ],
}
ENRICHED = [
    {"receipt": RECEIPT, "fiscal_data": FISCAL_DATA, "error": None},
    {"receipt": {"key": "test-key-2", "kktOwner": "ИП Тест", "totalSum": 100},
     "fiscal_data": None, "error": "Ресурс не найден (HTTP 404)."},
]


def read_csv(path):
    raw = path.read_bytes()
    text = raw.decode("utf-8-sig")
    return raw, list(csv.DictReader(io.StringIO(text), delimiter=";"))


class WriteFilesTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)

    def test_json_keeps_unicode_and_structure(self):
        path = self.dir / "receipts_full.json"
        exporter.write_json(path, ENRICHED)
        text = path.read_text(encoding="utf-8")
        self.assertIn("Молоко", text)
        self.assertEqual(json.loads(text), ENRICHED)

    def test_summary_csv(self):
        path = self.dir / "receipts_summary.csv"
        exporter.write_summary_csv(path, ENRICHED)
        raw, rows = read_csv(path)

        self.assertTrue(raw.startswith(b"\xef\xbb\xbf"))
        self.assertTrue(raw[3:].startswith(";".join(exporter.SUMMARY_FIELDS).encode()))
        self.assertEqual(len(rows), 2)
        first, second = rows
        self.assertEqual(first["key"], "test-key-1")
        self.assertEqual(first["receiptDateTime"], "2026-09-10T11:59:00")
        self.assertEqual(first["seller"], "ООО «Тест; кавычки»")
        self.assertEqual(first["sellerInn"], "1111111111")
        self.assertEqual(first["totalSum"], "15050")
        self.assertEqual(first["itemsCount"], "2")
        self.assertEqual(first["detailStatus"], "OK")
        self.assertEqual(second["seller"], "ИП Тест")
        self.assertEqual(second["totalSum"], "100")
        self.assertEqual(second["itemsCount"], "0")
        self.assertEqual(second["detailStatus"], "Ресурс не найден (HTTP 404).")

    def test_items_csv(self):
        path = self.dir / "receipt_items.csv"
        exporter.write_items_csv(path, ENRICHED)
        raw, rows = read_csv(path)

        self.assertTrue(raw.startswith(b"\xef\xbb\xbf"))
        self.assertEqual(list(rows[0].keys()), exporter.ITEM_FIELDS)
        self.assertEqual(len(rows), 2)
        self.assertEqual([r["name"] for r in rows], ["Молоко", "Хлеб"])
        self.assertEqual([r["itemIndex"] for r in rows], ["1", "2"])
        self.assertEqual(rows[0]["receiptKey"], "test-key-1")
        self.assertEqual(rows[0]["receiptTotalSum"], "15050")
        self.assertEqual(rows[0]["ndsSum"], "911")
        self.assertEqual(rows[1]["nds"], "")

    def test_csv_columns_unchanged_from_v01(self):
        self.assertEqual(exporter.SUMMARY_FIELDS, [
            "key", "createdDate", "receiveDate", "receiptDateTime", "seller",
            "sellerInn", "totalSum", "fiscalDocumentNumber", "fiscalDriveNumber",
            "sourceCode", "receiptState", "brandId", "itemsCount", "detailStatus"])
        self.assertEqual(exporter.ITEM_FIELDS, [
            "receiptKey", "receiptDateTime", "seller", "sellerInn",
            "receiptTotalSum", "itemIndex", "name", "price", "quantity", "sum",
            "providerInn", "paymentType", "paymentAgentByProductType",
            "productType", "nds", "ndsSum", "labelCodeProcessMode",
            "itemsQuantityMeasure"])


class RunTest(unittest.TestCase):
    """End-to-end run with the API layer mocked and a temporary output dir."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.out_dir = Path(tmp.name) / "nested" / "export"
        mock.patch.object(exporter.time, "sleep").start()
        mock.patch.dict(exporter.os.environ, {"FNS_TOKEN": "fake-token-123"}).start()
        self.urlopen = mock.patch.object(exporter._opener, "open").start()
        self.addCleanup(mock.patch.stopall)

    def run_main(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = exporter.main(argv)
        return code, out.getvalue(), err.getvalue()

    def fake_api(self, fail_key=None):
        from tests.test_api import FakeResponse, http_error

        def respond(req, timeout):
            payload = json.loads(req.data)
            if req.full_url == exporter.LIST_URL:
                return FakeResponse({"receipts": [RECEIPT, {"key": "k2"}]})
            if payload["key"] == fail_key:
                raise http_error(404)
            return FakeResponse(FISCAL_DATA)
        return respond

    def test_full_export_creates_output_dir_and_files(self):
        self.urlopen.side_effect = self.fake_api()
        code, out, err = self.run_main(["--output-dir", str(self.out_dir),
                                        "--date-from", "2026-09-01"])
        self.assertEqual(code, 0, err)
        names = sorted(p.name for p in self.out_dir.iterdir())
        self.assertEqual(names, ["receipt_items.csv", "receipts_full.json",
                                 "receipts_summary.csv"])
        for path in self.out_dir.iterdir():
            self.assertNotIn(b"fake-token-123", path.read_bytes())
        self.assertNotIn("fake-token-123", out + err)
        first_list_payload = json.loads(self.urlopen.call_args_list[0].args[0].data)
        self.assertEqual(first_list_payload["dateFrom"], "2026-09-01")
        self.assertIsNone(first_list_payload["dateTo"])

    def test_errors_json_only_when_errors(self):
        self.urlopen.side_effect = self.fake_api(fail_key="k2")
        code, _, err = self.run_main(["--output-dir", str(self.out_dir)])
        self.assertEqual(code, 0, err)
        errors = json.loads((self.out_dir / "errors.json").read_text(encoding="utf-8"))
        self.assertEqual([e["key"] for e in errors], ["k2"])
        self.assertNotIn("fake-token-123", json.dumps(errors))

        # A clean re-run into the same directory must not leave a stale errors.json.
        self.urlopen.side_effect = self.fake_api()
        code, _, err = self.run_main(["--output-dir", str(self.out_dir)])
        self.assertEqual(code, 0, err)
        self.assertFalse((self.out_dir / "errors.json").exists())

    def test_list_failure_exits_with_error_and_writes_nothing(self):
        from tests.test_api import http_error
        self.urlopen.side_effect = http_error(401)
        code, _, err = self.run_main(["--output-dir", str(self.out_dir)])
        self.assertEqual(code, 1)
        self.assertIn("истёк", err)
        self.assertEqual(list(self.out_dir.iterdir()), [])

    def test_period_without_receipts_creates_empty_export(self):
        from tests.test_api import FakeResponse
        self.urlopen.side_effect = [FakeResponse({"receipts": [], "hasMore": False})]
        code, out, err = self.run_main(["--output-dir", str(self.out_dir),
                                        "--date-from", "2026-09-01",
                                        "--date-to", "2026-09-17"])
        self.assertEqual(code, 0, err)
        self.assertIn("Найдено чеков: 0", out)
        self.assertEqual(self.urlopen.call_count, 1)
        self.assertEqual(
            json.loads((self.out_dir / "receipts_full.json").read_text(encoding="utf-8")), [])
        _, summary_rows = read_csv(self.out_dir / "receipts_summary.csv")
        _, item_rows = read_csv(self.out_dir / "receipt_items.csv")
        self.assertEqual((summary_rows, item_rows), ([], []))
        self.assertTrue((self.out_dir / "receipts_summary.csv").read_bytes()
                        .startswith(b"\xef\xbb\xbf" + ";".join(exporter.SUMMARY_FIELDS).encode()))
        self.assertFalse((self.out_dir / "errors.json").exists())

    def test_422_on_list_fails_with_instructions_and_writes_nothing(self):
        from tests.test_api import http_error
        self.urlopen.side_effect = http_error(422)
        code, _, err = self.run_main(["--output-dir", str(self.out_dir)])
        self.assertEqual(code, 1)
        self.assertIn("HTTP 422", err)
        self.assertIn("lkdr.nalog.ru", err)
        self.assertEqual(list(self.out_dir.iterdir()), [])

    def test_empty_token_exits_without_api_calls(self):
        with mock.patch.dict(exporter.os.environ, {"FNS_TOKEN": ""}), \
                mock.patch.object(exporter.getpass, "getpass", return_value="  "):
            code, _, err = self.run_main(["--output-dir", str(self.out_dir)])
        self.assertEqual(code, 2)
        self.assertIn("Пустой token", err)
        self.urlopen.assert_not_called()

    def test_output_dir_that_is_a_file_is_reported(self):
        self.out_dir.parent.mkdir(parents=True)
        self.out_dir.write_text("not a directory")
        code, _, err = self.run_main(["--output-dir", str(self.out_dir)])
        self.assertEqual(code, 2)
        self.assertIn("Не удалось создать каталог", err)
        self.urlopen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
