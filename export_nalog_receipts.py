#!/usr/bin/env python3
# Export receipts from ФНС "Мои чеки онлайн" (lkdr.nalog.ru).
# The Bearer token is entered locally and is never written to disk.

import argparse
import csv
import getpass
import http.client
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

BASE = "https://lkdr.nalog.ru/api"
LIST_URL = f"{BASE}/v1/receipt"
DETAIL_URL = f"{BASE}/v1/receipt/fiscal_data"

PAGE_SIZE = 10          # exact page size observed in the web app
REQUEST_DELAY = 0.08    # gentle delay between API calls
TIMEOUT = 30
MAX_RESPONSE_BYTES = 8 * 1024 * 1024


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never forward a receipt-session bearer token to a redirect target."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_opener = urllib.request.build_opener(NoRedirect, urllib.request.ProxyHandler({}))

MAX_ATTEMPTS = 3        # 1 request + up to 2 retries
BACKOFF_BASE = 1.0      # seconds between attempts: 1, 2
MAX_RETRY_AFTER = 30.0  # upper bound for a server-provided Retry-After
RETRYABLE_STATUSES = {429, 500, 502, 503, 504}

# The lkdr.nalog.ru web app (main.*.chunk.js) turns every error of
# POST /v1/receipt into an empty list EXCEPT 422, which it re-throws on the
# first page and on "load more" alike; the UI then opens the
# "Настройки отображения данных" dialog (show all receipts for a phone/email
# identifier or only new ones) and calls /v1/identifiers/receipt_expiration_date.
# So 422 means "an action is required in the account", not "no receipts":
# an empty result is a successful response with "receipts": [].
LIST_422_HINT = (
    "Сервис требует действия в личном кабинете: откройте https://lkdr.nalog.ru "
    "в браузере, выберите вариант в окне «Настройки отображения данных», "
    "если оно появится, и повторите экспорт."
)

TOKEN_ENV_VAR = "FNS_TOKEN"
DEFAULT_OUT_DIR = Path("nalog_receipts_export")

RAW_JSON_NAME = "receipts_full.json"
SUMMARY_CSV_NAME = "receipts_summary.csv"
ITEMS_CSV_NAME = "receipt_items.csv"
ERRORS_JSON_NAME = "errors.json"

# The web app formats the filter dates with date-fns "yyyy-MM-dd".
API_DATE_FORMAT = "%Y-%m-%d"
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

HTTP_STATUS_MESSAGES = {
    400: "Некорректный запрос (HTTP 400). Проверьте параметры, например даты.",
    401: "Bearer token недействителен или истёк (HTTP 401). "
         "Получите новый token и запустите экспорт повторно.",
    403: "Доступ запрещён (HTTP 403). Сессия могла быть завершена "
         "или у token нет доступа к этому ресурсу.",
    404: "Ресурс не найден (HTTP 404). Возможно, API сервиса изменился.",
    422: "Сервис отклонил запрос (HTTP 422).",
    429: "Сервис ограничил частоту запросов (HTTP 429). "
         "Повторите позже или увеличьте --request-delay.",
    500: "Внутренняя ошибка сервиса (HTTP 500).",
    502: "Сервис временно недоступен (HTTP 502).",
    503: "Сервис временно недоступен (HTTP 503).",
    504: "Сервис не ответил вовремя (HTTP 504).",
}

SUMMARY_FIELDS = [
    "key",
    "createdDate",
    "receiveDate",
    "receiptDateTime",
    "seller",
    "sellerInn",
    "totalSum",
    "fiscalDocumentNumber",
    "fiscalDriveNumber",
    "sourceCode",
    "receiptState",
    "brandId",
    "itemsCount",
    "detailStatus",
]

ITEM_FIELDS = [
    "receiptKey",
    "receiptDateTime",
    "seller",
    "sellerInn",
    "receiptTotalSum",
    "itemIndex",
    "name",
    "price",
    "quantity",
    "sum",
    "providerInn",
    "paymentType",
    "paymentAgentByProductType",
    "productType",
    "nds",
    "ndsSum",
    "labelCodeProcessMode",
    "itemsQuantityMeasure",
]


class ApiError(Exception):
    """API request failed. The message never contains the token."""

    def __init__(self, message, status=None, retryable=False, retry_after=None):
        super().__init__(message)
        self.status = status
        self.retryable = retryable
        self.retry_after = retry_after


# ---------------------------------------------------------------- CLI / input

def iso_date(value):
    """argparse type: strict YYYY-MM-DD, returned unchanged as a string."""
    if not DATE_RE.match(value):
        raise argparse.ArgumentTypeError(
            f"некорректная дата {value!r}: ожидается формат YYYY-MM-DD")
    try:
        datetime.strptime(value, API_DATE_FORMAT)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"некорректная дата {value!r}: такой даты не существует") from None
    return value


def request_delay(value):
    try:
        delay = float(value)
    except ValueError:
        delay = -1.0
    if not 0 <= delay <= 60:  # also rejects nan
        raise argparse.ArgumentTypeError(
            f"ожидается число секунд от 0 до 60, получено {value!r}")
    return delay


def build_parser():
    parser = argparse.ArgumentParser(
        prog="export_nalog_receipts.py",
        description=(
            "Локальный экспорт чеков из сервиса ФНС «Мои чеки онлайн» "
            "(lkdr.nalog.ru) в JSON и CSV."
        ),
        epilog=(
            "Bearer token берётся из переменной окружения FNS_TOKEN; "
            "если она не задана, token запрашивается скрыто в терминале. "
            "Token не сохраняется в файлы.\n\n"
            "Примеры:\n"
            "  python3 export_nalog_receipts.py\n"
            "  python3 export_nalog_receipts.py --date-from 2026-09-01 "
            "--date-to 2026-09-17\n"
            "  python3 export_nalog_receipts.py --output-dir ~/Downloads/fns-receipts"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--date-from", type=iso_date, metavar="YYYY-MM-DD",
        help="начальная дата периода (передаётся в API как есть). "
             "По умолчанию без ограничения.")
    parser.add_argument(
        "--date-to", type=iso_date, metavar="YYYY-MM-DD",
        help="конечная дата периода (передаётся в API как есть). "
             "По умолчанию без ограничения.")
    parser.add_argument(
        "--output-dir", type=Path, default=DEFAULT_OUT_DIR, metavar="PATH",
        help="каталог для результатов; создаётся автоматически. "
             "Поддерживаются ~, относительные и абсолютные пути. "
             f"По умолчанию: ./{DEFAULT_OUT_DIR}")
    parser.add_argument(
        "--request-delay", type=request_delay, default=REQUEST_DELAY,
        metavar="SECONDS",
        help=f"пауза между запросами к API в секундах (0–60). "
             f"По умолчанию {REQUEST_DELAY}.")
    return parser


def parse_args(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        validate_dates(args.date_from, args.date_to)
    except ValueError as e:
        parser.error(str(e))
    args.output_dir = args.output_dir.expanduser()
    return args


def validate_dates(date_from, date_to):
    # Both are validated YYYY-MM-DD strings, so string order == date order.
    if date_from and date_to and date_from > date_to:
        raise ValueError(
            f"--date-from ({date_from}) не может быть позже --date-to ({date_to})")


def normalize_token(raw):
    raw = (raw or "").strip()
    scheme, _, rest = raw.partition(" ")
    if scheme.lower() == "bearer":
        raw = rest.strip()
    return raw


def read_token(environ=None):
    """FNS_TOKEN from the environment, otherwise a hidden terminal prompt."""
    environ = os.environ if environ is None else environ
    token = normalize_token(environ.get(TOKEN_ENV_VAR))
    if token:
        print(f"Token взят из переменной окружения {TOKEN_ENV_VAR}.")
        return token
    print("Токен вводится только в терминале и НЕ сохраняется в файлы.")
    return normalize_token(getpass.getpass("Вставьте Bearer token: "))


# ------------------------------------------------------------------------ API

def redact(text, token):
    if token:
        text = text.replace(token, "***")
    return text


def parse_retry_after(value):
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    if not seconds >= 0:  # also rejects nan
        return None
    return min(seconds, MAX_RETRY_AFTER)


def service_error_text(body):
    """Short error description from a response body.

    For JSON error objects only "code" and "message" are kept: other fields,
    such as "additionalInfo" of HTTP 422, may contain the user's phone or email.
    """
    try:
        data = json.loads(body)
    except ValueError:
        return body[:500]
    if not isinstance(data, dict):
        return body[:500]
    return " ".join(str(data[k]) for k in ("code", "message") if data.get(k))[:500]


def http_error_to_api_error(e, token):
    try:
        body = e.read(MAX_RESPONSE_BYTES + 1).decode("utf-8", errors="replace")
    except Exception:
        body = ""
    base = HTTP_STATUS_MESSAGES.get(e.code, f"Ошибка HTTP {e.code}.")
    details = service_error_text(body) if body else ""
    message = f"{base} Ответ сервиса: {details}" if details else base
    retry_after = None
    if e.code == 429 and e.headers is not None:
        retry_after = parse_retry_after(e.headers.get("Retry-After"))
    return ApiError(
        redact(message, token),
        status=e.code,
        retryable=e.code in RETRYABLE_STATUSES,
        retry_after=retry_after,
    )


def request_json_once(url, token, payload):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json;charset=UTF-8",
            "Accept": "application/json",
            "Origin": "https://lkdr.nalog.ru",
            "Referer": "https://lkdr.nalog.ru/",
            "User-Agent": "Mozilla/5.0",
        },
    )
    try:
        with _opener.open(req, timeout=TIMEOUT) as resp:
            raw = resp.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise ApiError("Ответ сервиса превышает допустимый размер.")
    except urllib.error.HTTPError as e:
        raise http_error_to_api_error(e, token) from None
    except urllib.error.URLError as e:
        raise ApiError(
            redact(f"Сетевая ошибка (нет соединения, DNS или таймаут): {e.reason}", token),
            retryable=True) from None
    except (OSError, http.client.HTTPException) as e:
        # Timeouts and broken connections while reading the body are not
        # wrapped in URLError.
        raise ApiError(
            redact(f"Сетевая ошибка или таймаут: {e}", token),
            retryable=True) from None

    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise ApiError("Сервис вернул некорректный JSON.") from None


def request_json(url, token, payload, max_attempts=MAX_ATTEMPTS):
    """POST JSON with bounded exponential backoff for transient failures."""
    for attempt in range(1, max_attempts + 1):
        try:
            return request_json_once(url, token, payload)
        except ApiError as e:
            if not e.retryable or attempt == max_attempts:
                raise
            delay = BACKOFF_BASE * 2 ** (attempt - 1)
            if e.retry_after is not None:
                delay = max(delay, e.retry_after)
            print(f"\n  {e} Повтор через {delay:g} с "
                  f"(попытка {attempt + 1}/{max_attempts})...", file=sys.stderr)
            time.sleep(delay)


def build_list_payload(limit, offset, date_from=None, date_to=None):
    # Same shape the working v0.1 script sent; dates are YYYY-MM-DD or null,
    # exactly as the lkdr.nalog.ru web app sends them.
    return {
        "limit": limit,
        "offset": offset,
        "dateFrom": date_from,
        "dateTo": date_to,
        "orderBy": "CREATED_DATE:DESC",
        "inn": None,
        "kktOwner": "",
    }


def get_receipts(token, date_from=None, date_to=None, delay=REQUEST_DELAY):
    receipts = []
    seen_keys = set()
    previous_page = None
    offset = 0

    while True:
        payload = build_list_payload(PAGE_SIZE, offset, date_from, date_to)
        # Every API error is fatal here, 422 included (see LIST_422_HINT), so an
        # error is never turned into an empty or truncated list.
        try:
            response = request_json(LIST_URL, token, payload)
        except ApiError as e:
            if e.status == 422:
                raise ApiError(f"{e} {LIST_422_HINT}", status=422) from None
            raise

        page = response.get("receipts", []) if isinstance(response, dict) else None
        if not isinstance(page, list):
            raise ApiError("Неожиданный ответ /v1/receipt: поле receipts не является списком")

        page_keys = {r.get("key") for r in page if isinstance(r, dict) and r.get("key")}
        repeated = page_keys <= seen_keys if page_keys else page == previous_page
        if page and repeated:
            # Guards against an endless loop if the service ignores offset.
            print("\n  Предупреждение: сервис вернул страницу без новых чеков; "
                  "получение списка остановлено.", file=sys.stderr)
            break
        seen_keys |= page_keys
        previous_page = page

        receipts.extend(page)
        print(f"  получено: {len(receipts)}", end="\r", flush=True)

        # Like the web app: the next offset is the number of receipts received,
        # and hasMore == false means there is nothing left to request.
        if len(page) < PAGE_SIZE or response.get("hasMore") is False:
            break
        offset += len(page)
        time.sleep(delay)

    return receipts


def get_receipt_detail(token, receipt):
    """Return (fiscal_data, error, errors_json_entry); never raises ApiError."""
    key = receipt.get("key") if isinstance(receipt, dict) else None
    if not key:
        error = "missing key"
        return None, error, {"key": None, "receipt": receipt, "error": error}
    try:
        detail = request_json(DETAIL_URL, token, {"key": key})
    except ApiError as e:
        error = str(e)
        return None, error, {"key": key, "error": error}
    if not isinstance(detail, dict):
        error = "Неожиданный ответ /v1/receipt/fiscal_data: ожидался JSON-объект"
        return None, error, {"key": key, "error": error}
    return detail, None, None


def enrich_receipts(token, receipts, delay=REQUEST_DELAY):
    enriched = []
    errors = []

    for i, receipt in enumerate(receipts, 1):
        detail, error, error_entry = get_receipt_detail(token, receipt)
        if error_entry is not None:
            errors.append(error_entry)
        if error_entry is None or error_entry["key"]:
            time.sleep(delay)  # only after an actual API call

        enriched.append({
            "receipt": receipt,
            "fiscal_data": detail,
            "error": error,
        })
        print(f"  обработано: {i}/{len(receipts)}", end="\r", flush=True)

    return enriched, errors


# --------------------------------------------------------------------- Export

def write_json(path, data):
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def summary_row(entry):
    r = entry["receipt"] or {}
    d = entry["fiscal_data"] or {}
    items = d.get("items") or []
    return {
        "key": r.get("key"),
        "createdDate": r.get("createdDate"),
        "receiveDate": r.get("receiveDate"),
        "receiptDateTime": d.get("dateTime"),
        "seller": d.get("user") or r.get("kktOwner"),
        "sellerInn": d.get("userInn") or r.get("kktOwnerInn"),
        "totalSum": d.get("totalSum", r.get("totalSum")),
        "fiscalDocumentNumber": r.get("fiscalDocumentNumber"),
        "fiscalDriveNumber": r.get("fiscalDriveNumber"),
        "sourceCode": r.get("sourceCode"),
        "receiptState": r.get("receiptState"),
        "brandId": r.get("brandId"),
        "itemsCount": len(items) if isinstance(items, list) else "",
        "detailStatus": "OK" if entry["fiscal_data"] is not None else entry["error"],
    }


def item_rows(entry):
    r = entry["receipt"] or {}
    d = entry["fiscal_data"] or {}
    items = d.get("items") or []
    if not isinstance(items, list):
        return

    for idx, item in enumerate(items, 1):
        if not isinstance(item, dict):
            continue
        yield {
            "receiptKey": r.get("key"),
            "receiptDateTime": d.get("dateTime"),
            "seller": d.get("user") or r.get("kktOwner"),
            "sellerInn": d.get("userInn") or r.get("kktOwnerInn"),
            "receiptTotalSum": d.get("totalSum", r.get("totalSum")),
            "itemIndex": idx,
            "name": item.get("name"),
            "price": item.get("price"),
            "quantity": item.get("quantity"),
            "sum": item.get("sum"),
            "providerInn": item.get("providerInn"),
            "paymentType": item.get("paymentType"),
            "paymentAgentByProductType": item.get("paymentAgentByProductType"),
            "productType": item.get("productType"),
            "nds": item.get("nds"),
            "ndsSum": item.get("ndsSum"),
            "labelCodeProcessMode": item.get("labelCodeProcessMode"),
            "itemsQuantityMeasure": item.get("itemsQuantityMeasure"),
        }


def write_csv(path, fieldnames, rows):
    # UTF-8 with BOM and ";" so that Excel opens the file correctly.
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, delimiter=";")
        w.writeheader()
        # Spreadsheet applications interpret these prefixes as formulas.
        for row in rows:
            w.writerow({key: ("'" + value if isinstance(value, str)
                              and value.lstrip().startswith(("=", "+", "-", "@"))
                              else value) for key, value in row.items()})


def write_summary_csv(path, enriched):
    write_csv(path, SUMMARY_FIELDS, (summary_row(e) for e in enriched))


def write_items_csv(path, enriched):
    write_csv(path, ITEM_FIELDS, (row for e in enriched for row in item_rows(e)))


# ----------------------------------------------------------------------- Main

def run(args):
    print("ФНС «Мои чеки онлайн» — локальный экспорт")
    if args.date_from or args.date_to:
        print(f"Период: {args.date_from or '…'} — {args.date_to or '…'}")

    token = read_token()
    if not token:
        print("Пустой token.", file=sys.stderr)
        return 2

    out_dir = args.output_dir
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        print(f"Не удалось создать каталог {out_dir}: {e}", file=sys.stderr)
        return 2
    raw_json = out_dir / RAW_JSON_NAME
    summary_csv = out_dir / SUMMARY_CSV_NAME
    items_csv = out_dir / ITEMS_CSV_NAME
    errors_json = out_dir / ERRORS_JSON_NAME

    print("\n1/3 Получаю список чеков...")
    try:
        receipts = get_receipts(token, args.date_from, args.date_to,
                                delay=args.request_delay)
    except ApiError as e:
        print(f"\nНе удалось получить список чеков. {e}", file=sys.stderr)
        return 1
    print(f"\nНайдено чеков: {len(receipts)}")

    print("\n2/3 Получаю состав каждого чека...")
    enriched, errors = enrich_receipts(token, receipts, delay=args.request_delay)
    print()

    try:
        write_json(raw_json, enriched)
        if errors:
            write_json(errors_json, errors)
        elif errors_json.is_file():
            errors_json.unlink()  # stale file from a previous run into the same directory

        print("\n3/3 Формирую CSV...")
        write_summary_csv(summary_csv, enriched)
        write_items_csv(items_csv, enriched)
    except OSError as e:
        print(f"Не удалось записать результаты в {out_dir}: {e}", file=sys.stderr)
        return 1

    print("\nГотово.")
    print(f"  Полный JSON: {raw_json}")
    print(f"  Сводка чеков: {summary_csv}")
    print(f"  Позиции чеков: {items_csv}")
    if errors:
        print(f"  Ошибки отдельных чеков: {errors_json}")
        print(f"  Не удалось получить детализацию: {len(errors)}")
    else:
        print("  Все чеки выгружены с детализацией.")
    return 0


def main(argv=None):
    args = parse_args(argv)
    try:
        return run(args)
    except KeyboardInterrupt:
        print("\nПрервано пользователем.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
