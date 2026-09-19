import email.message
import io
import json
import socket
import unittest
import urllib.error
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

import export_nalog_receipts as exporter

TOKEN = "test-token-not-real"


class FakeResponse:
    def __init__(self, body):
        self._body = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")

    def read(self, size=-1):
        return self._body if size < 0 else self._body[:size]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def http_error(code, body=b"", headers=None):
    msg = email.message.Message()
    for name, value in (headers or {}).items():
        msg[name] = value
    return urllib.error.HTTPError(
        exporter.LIST_URL, code, "error", msg, io.BytesIO(body))


class ApiTestCase(unittest.TestCase):
    """Patches the network and sleeping; no test talks to the real service."""

    def setUp(self):
        self.urlopen = mock.patch.object(exporter._opener, "open").start()
        self.sleep = mock.patch.object(exporter.time, "sleep").start()
        self.addCleanup(mock.patch.stopall)
        for stream in (redirect_stdout, redirect_stderr):
            ctx = stream(io.StringIO())
            ctx.__enter__()
            self.addCleanup(ctx.__exit__, None, None, None)


class BuildListPayloadTest(unittest.TestCase):
    def test_default_payload_matches_v01(self):
        self.assertEqual(exporter.build_list_payload(10, 0), {
            "limit": 10,
            "offset": 0,
            "dateFrom": None,
            "dateTo": None,
            "orderBy": "CREATED_DATE:DESC",
            "inn": None,
            "kktOwner": "",
        })

    def test_payload_with_dates(self):
        payload = exporter.build_list_payload(10, 20, "2026-09-01", "2026-09-17")
        self.assertEqual(payload["offset"], 20)
        self.assertEqual(payload["dateFrom"], "2026-09-01")
        self.assertEqual(payload["dateTo"], "2026-09-17")


class RequestJsonTest(ApiTestCase):
    def test_success_and_request_shape(self):
        self.urlopen.return_value = FakeResponse({"receipts": []})
        result = exporter.request_json(exporter.LIST_URL, TOKEN, {"limit": 10})

        self.assertEqual(result, {"receipts": []})
        req = self.urlopen.call_args.args[0]
        self.assertEqual(req.get_method(), "POST")
        self.assertEqual(req.full_url, "https://lkdr.nalog.ru/api/v1/receipt")
        self.assertEqual(req.get_header("Authorization"), f"Bearer {TOKEN}")
        self.assertEqual(req.get_header("Content-type"), "application/json;charset=UTF-8")
        self.assertEqual(json.loads(req.data), {"limit": 10})
        self.assertEqual(self.urlopen.call_args.kwargs["timeout"], exporter.TIMEOUT)

    def test_non_retryable_statuses(self):
        expected = {400: "HTTP 400", 401: "token", 403: "HTTP 403", 404: "HTTP 404"}
        for code, fragment in expected.items():
            with self.subTest(code=code):
                self.urlopen.reset_mock()
                self.urlopen.side_effect = http_error(code, b'{"message":"x"}')
                with self.assertRaises(exporter.ApiError) as ctx:
                    exporter.request_json(exporter.LIST_URL, TOKEN, {})
                self.assertEqual(ctx.exception.status, code)
                self.assertIn(fragment, str(ctx.exception))
                self.assertEqual(self.urlopen.call_count, 1)

    def test_401_message_mentions_expired_token(self):
        self.urlopen.side_effect = http_error(401)
        with self.assertRaises(exporter.ApiError) as ctx:
            exporter.request_json(exporter.LIST_URL, TOKEN, {})
        self.assertIn("истёк", str(ctx.exception))

    def test_retryable_statuses_are_retried_then_fail(self):
        for code in (429, 500, 502, 503, 504):
            with self.subTest(code=code):
                self.urlopen.reset_mock()
                self.sleep.reset_mock()
                self.urlopen.side_effect = http_error(code)
                with self.assertRaises(exporter.ApiError) as ctx:
                    exporter.request_json(exporter.LIST_URL, TOKEN, {})
                self.assertEqual(ctx.exception.status, code)
                self.assertEqual(self.urlopen.call_count, exporter.MAX_ATTEMPTS)
                self.assertEqual([c.args[0] for c in self.sleep.call_args_list], [1.0, 2.0])

    def test_429_message_mentions_rate_limit(self):
        self.urlopen.side_effect = http_error(429)
        with self.assertRaises(exporter.ApiError) as ctx:
            exporter.request_json(exporter.LIST_URL, TOKEN, {})
        self.assertIn("частоту запросов", str(ctx.exception))

    def test_retry_after_is_respected_within_limit(self):
        self.urlopen.side_effect = [
            http_error(429, headers={"Retry-After": "5"}),
            FakeResponse({"ok": True}),
        ]
        self.assertEqual(exporter.request_json(exporter.LIST_URL, TOKEN, {}), {"ok": True})
        self.sleep.assert_called_once_with(5.0)

    def test_huge_retry_after_is_capped(self):
        self.urlopen.side_effect = [
            http_error(429, headers={"Retry-After": "86400"}),
            FakeResponse({"ok": True}),
        ]
        exporter.request_json(exporter.LIST_URL, TOKEN, {})
        self.sleep.assert_called_once_with(exporter.MAX_RETRY_AFTER)

    def test_invalid_retry_after_falls_back_to_backoff(self):
        self.urlopen.side_effect = [
            http_error(429, headers={"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}),
            FakeResponse({"ok": True}),
        ]
        exporter.request_json(exporter.LIST_URL, TOKEN, {})
        self.sleep.assert_called_once_with(1.0)

    def test_transient_error_then_success(self):
        self.urlopen.side_effect = [http_error(503), http_error(502), FakeResponse({"ok": 1})]
        self.assertEqual(exporter.request_json(exporter.LIST_URL, TOKEN, {}), {"ok": 1})
        self.assertEqual(self.urlopen.call_count, 3)

    def test_timeout_is_retried(self):
        self.urlopen.side_effect = [socket.timeout("timed out"), FakeResponse({"ok": 1})]
        self.assertEqual(exporter.request_json(exporter.LIST_URL, TOKEN, {}), {"ok": 1})

    def test_network_error_exhausts_attempts(self):
        self.urlopen.side_effect = urllib.error.URLError("nodename nor servname provided")
        with self.assertRaises(exporter.ApiError) as ctx:
            exporter.request_json(exporter.LIST_URL, TOKEN, {})
        self.assertIn("Сетевая ошибка", str(ctx.exception))
        self.assertIsNone(ctx.exception.status)
        self.assertEqual(self.urlopen.call_count, exporter.MAX_ATTEMPTS)

    def test_invalid_json_is_not_retried(self):
        self.urlopen.return_value = FakeResponse(b"<html>not json</html>")
        with self.assertRaises(exporter.ApiError) as ctx:
            exporter.request_json(exporter.LIST_URL, TOKEN, {})
        self.assertIn("некорректный JSON", str(ctx.exception))
        self.assertEqual(self.urlopen.call_count, 1)

    def test_token_is_redacted_from_error_messages(self):
        self.urlopen.side_effect = http_error(400, f"bad header Bearer {TOKEN}".encode())
        with self.assertRaises(exporter.ApiError) as ctx:
            exporter.request_json(exporter.LIST_URL, TOKEN, {})
        self.assertNotIn(TOKEN, str(ctx.exception))


def receipts_page(start, count):
    return {"receipts": [{"key": f"k{i}"} for i in range(start, start + count)]}


class GetReceiptsTest(ApiTestCase):
    def sent_payloads(self):
        return [json.loads(c.args[0].data) for c in self.urlopen.call_args_list]

    def test_pagination_collects_all_pages_without_gaps(self):
        self.urlopen.side_effect = [
            FakeResponse(receipts_page(0, 10)),
            FakeResponse(receipts_page(10, 10)),
            FakeResponse(receipts_page(20, 3)),
        ]
        receipts = exporter.get_receipts(TOKEN, "2026-09-01", "2026-09-17")

        self.assertEqual([r["key"] for r in receipts], [f"k{i}" for i in range(23)])
        payloads = self.sent_payloads()
        self.assertEqual([p["offset"] for p in payloads], [0, 10, 20])
        self.assertTrue(all(p["limit"] == 10 for p in payloads))
        self.assertTrue(all(p["dateFrom"] == "2026-09-01" for p in payloads))
        self.assertTrue(all(p["dateTo"] == "2026-09-17" for p in payloads))

    def test_has_more_false_stops_without_extra_request(self):
        page = receipts_page(0, 10)
        page["hasMore"] = False
        self.urlopen.side_effect = [FakeResponse(page)]
        self.assertEqual(len(exporter.get_receipts(TOKEN)), 10)
        self.assertEqual(self.urlopen.call_count, 1)

    def test_offset_advances_by_received_count(self):
        # Defensive: if the service ignored limit and returned more receipts.
        self.urlopen.side_effect = [
            FakeResponse(receipts_page(0, 12)),
            FakeResponse(receipts_page(12, 3)),
        ]
        receipts = exporter.get_receipts(TOKEN)
        self.assertEqual([r["key"] for r in receipts], [f"k{i}" for i in range(15)])
        self.assertEqual([p["offset"] for p in self.sent_payloads()], [0, 12])

    def test_exactly_full_last_page_requests_one_empty_page(self):
        self.urlopen.side_effect = [
            FakeResponse(receipts_page(0, 10)),
            FakeResponse({"receipts": []}),
        ]
        receipts = exporter.get_receipts(TOKEN)
        self.assertEqual(len(receipts), 10)
        self.assertEqual([p["offset"] for p in self.sent_payloads()], [0, 10])

    def test_empty_first_page_is_an_empty_result(self):
        self.urlopen.side_effect = [FakeResponse({"receipts": [], "hasMore": False})]
        self.assertEqual(exporter.get_receipts(TOKEN, "2026-09-01", "2026-09-17"), [])
        self.assertEqual(self.urlopen.call_count, 1)

    def test_repeated_page_stops_instead_of_looping_forever(self):
        self.urlopen.side_effect = lambda *a, **kw: FakeResponse(receipts_page(0, 10))
        receipts = exporter.get_receipts(TOKEN)
        self.assertEqual(len(receipts), 10)
        self.assertEqual(self.urlopen.call_count, 2)

    def test_keyless_pages_do_not_loop_forever(self):
        self.urlopen.side_effect = lambda *a, **kw: FakeResponse({"receipts": [{}] * 10})
        receipts = exporter.get_receipts(TOKEN)
        self.assertEqual(len(receipts), 10)
        self.assertEqual(self.urlopen.call_count, 2)

    def test_unexpected_response_raises(self):
        for body in ({"receipts": "oops"}, ["not", "a", "dict"]):
            with self.subTest(body=body):
                self.urlopen.side_effect = [FakeResponse(body)]
                with self.assertRaises(exporter.ApiError):
                    exporter.get_receipts(TOKEN)

    def test_list_failure_propagates(self):
        self.urlopen.side_effect = http_error(401)
        with self.assertRaises(exporter.ApiError) as ctx:
            exporter.get_receipts(TOKEN)
        self.assertEqual(ctx.exception.status, 401)

    # lkdr.nalog.ru web app: 422 from /v1/receipt is re-thrown and opens the
    # "Настройки отображения данных" dialog; it is not an empty list.
    def test_422_on_first_page_is_an_error(self):
        self.urlopen.side_effect = [http_error(422)]
        with self.assertRaises(exporter.ApiError) as ctx:
            exporter.get_receipts(TOKEN)
        self.assertEqual(ctx.exception.status, 422)
        self.assertIn("Настройки отображения данных", str(ctx.exception))
        self.assertEqual(self.urlopen.call_count, 1)  # not retried

    def test_422_after_first_page_is_an_error_too(self):
        self.urlopen.side_effect = [FakeResponse(receipts_page(0, 10)), http_error(422)]
        with self.assertRaises(exporter.ApiError) as ctx:
            exporter.get_receipts(TOKEN)
        self.assertEqual(ctx.exception.status, 422)

    def test_422_additional_info_is_not_echoed(self):
        # Synthetic body shaped like the web app expects: code, message and
        # additionalInfo with a JSON-encoded identifier (phone or email).
        body = json.dumps({
            "code": "receipt.expiration.date.required",
            "message": "choose display settings",
            "additionalInfo": {"x": '{"login": "+70000000000", "type": "SMS"}'},
        }).encode()
        self.urlopen.side_effect = [http_error(422, body)]
        with self.assertRaises(exporter.ApiError) as ctx:
            exporter.get_receipts(TOKEN)
        message = str(ctx.exception)
        self.assertIn("receipt.expiration.date.required", message)
        self.assertIn("choose display settings", message)
        self.assertNotIn("70000000000", message)


class ReceiptDetailTest(ApiTestCase):
    def test_detail_uses_key(self):
        self.urlopen.return_value = FakeResponse({"items": []})
        detail, error, entry = exporter.get_receipt_detail(TOKEN, {"key": "abc"})
        self.assertEqual((detail, error, entry), ({"items": []}, None, None))
        req = self.urlopen.call_args.args[0]
        self.assertEqual(req.full_url, "https://lkdr.nalog.ru/api/v1/receipt/fiscal_data")
        self.assertEqual(json.loads(req.data), {"key": "abc"})

    def test_missing_key_skips_request(self):
        receipt = {"totalSum": 100}
        detail, error, entry = exporter.get_receipt_detail(TOKEN, receipt)
        self.assertIsNone(detail)
        self.assertEqual(error, "missing key")
        self.assertEqual(entry, {"key": None, "receipt": receipt, "error": "missing key"})
        self.urlopen.assert_not_called()

    def test_non_object_detail_is_an_error(self):
        self.urlopen.return_value = FakeResponse([1, 2, 3])
        detail, error, entry = exporter.get_receipt_detail(TOKEN, {"key": "abc"})
        self.assertIsNone(detail)
        self.assertEqual(entry["key"], "abc")

    def test_detail_422_has_no_list_specific_hint(self):
        self.urlopen.side_effect = [http_error(422)]
        _, error, _ = exporter.get_receipt_detail(TOKEN, {"key": "abc"})
        self.assertIn("HTTP 422", error)
        self.assertNotIn("Настройки отображения данных", error)

    def test_one_failing_receipt_does_not_stop_others(self):
        def respond(req, timeout):
            key = json.loads(req.data)["key"]
            if key == "bad":
                raise http_error(404)
            return FakeResponse({"items": [{"name": key}]})

        self.urlopen.side_effect = respond
        receipts = [{"key": "a"}, {"key": "bad"}, {}, {"key": "c"}]
        enriched, errors = exporter.enrich_receipts(TOKEN, receipts)

        self.assertEqual(len(enriched), 4)
        self.assertEqual(enriched[0]["fiscal_data"], {"items": [{"name": "a"}]})
        self.assertIsNone(enriched[1]["fiscal_data"])
        self.assertIn("HTTP 404", enriched[1]["error"])
        self.assertEqual(enriched[2]["error"], "missing key")
        self.assertEqual(enriched[3]["fiscal_data"], {"items": [{"name": "c"}]})
        self.assertEqual([e["key"] for e in errors], ["bad", None])
        # No pause after the receipt without a key, since no request was made.
        self.assertEqual(self.sleep.call_count, 3)


if __name__ == "__main__":
    unittest.main()
