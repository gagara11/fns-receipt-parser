"""Persistent raw archive. All user filters are bound SQL parameters."""

import hashlib
import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path


_SAFE_SEQ = 9007199254740991
_MONEY_FIELDS = {"totalSum", "cashTotalSum", "ecashTotalSum", "prepaidSum", "price", "sum", "ndsSum"}
_ID_FIELDS = {"fiscalSign", "fiscalDocumentNumber", "fiscalDriveNumber"}


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _wire(value, key=None):
    if isinstance(value, dict):
        return {name: _wire(item, name) for name, item in value.items()}
    if isinstance(value, list):
        return [_wire(item) for item in value]
    if key in _MONEY_FIELDS and isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
        number = Decimal(str(value))
        if not number.is_finite():
            raise ValueError("Invalid monetary value in archive")
        return format(number, "f")
    if key in _ID_FIELDS and value is not None:
        return str(value)
    return value


def dates(date_from, date_to):
    for value in (date_from, date_to):
        if value is not None and date.fromisoformat(value).isoformat() != value:
            raise ValueError("Dates must use YYYY-MM-DD")
    if date_from and date_to and date_from > date_to:
        raise ValueError("date_from must not exceed date_to")


class Store:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS receipts (
                    key TEXT PRIMARY KEY, purchased_at TEXT, seller TEXT,
                    raw TEXT NOT NULL, detail TEXT, error TEXT
                );
                CREATE INDEX IF NOT EXISTS receipts_date ON receipts(purchased_at);
                CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS archive_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            """)
            columns = {row[1] for row in db.execute("PRAGMA table_info(receipts)")}
            if "seq" not in columns:
                db.execute("ALTER TABLE receipts ADD COLUMN seq INTEGER")
            db.execute("INSERT OR IGNORE INTO archive_meta VALUES ('archive_id',?)", (str(uuid.uuid4()),))
            rows = db.execute("SELECT key FROM receipts WHERE seq IS NULL ORDER BY key").fetchall()
            next_seq = db.execute("SELECT COALESCE(MAX(seq),0) FROM receipts").fetchone()[0]
            for row in rows:
                next_seq += 1
                if next_seq > _SAFE_SEQ:
                    raise ValueError("Archive revision limit exceeded")
                db.execute("UPDATE receipts SET seq=? WHERE key=?", (next_seq, row["key"]))
            db.execute("CREATE INDEX IF NOT EXISTS receipts_seq ON receipts(seq)")
            db.execute("INSERT INTO archive_meta VALUES ('next_seq',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                       (str(next_seq),))

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def status(self):
        with self.connect() as db:
            result = {r["key"]: json.loads(r["value"]) for r in db.execute("SELECT * FROM state")}
            result.update(dict(db.execute("SELECT COUNT(*) AS receipts, "
                          "COUNT(detail) AS detailed_receipts FROM receipts").fetchone()))
            return result

    def state(self, **values):
        with self.connect() as db:
            db.executemany("INSERT INTO state VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                           [(k, json.dumps(v)) for k, v in values.items()])

    def has_detail(self, key):
        with self.connect() as db:
            return db.execute("SELECT 1 FROM receipts WHERE key=? AND detail IS NOT NULL", (key,)).fetchone() is not None

    def put(self, receipt, detail, error=None):
        d = detail or {}
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT purchased_at,seller,raw,detail,error,seq FROM receipts WHERE key=?",
                             (receipt["key"],)).fetchone()
            raw = json.dumps(receipt, ensure_ascii=False)
            raw_detail = json.dumps(detail, ensure_ascii=False) if detail is not None else (old["detail"] if old else None)
            new_error = error if error is not None else (None if detail is not None else (old["error"] if old else None))
            purchased_at = (d.get("dateTime") or receipt.get("createdDate")) if detail is not None else (
                old["purchased_at"] if old else receipt.get("createdDate"))
            seller = (d.get("user") or receipt.get("kktOwner", "")) if detail is not None else (
                old["seller"] if old else receipt.get("kktOwner", ""))
            changed = old is None or (raw, raw_detail, new_error) != (old["raw"], old["detail"], old["error"])
            if changed:
                seq = int(db.execute("SELECT value FROM archive_meta WHERE key='next_seq'").fetchone()[0]) + 1
                if seq > _SAFE_SEQ:
                    raise ValueError("Archive revision limit exceeded")
                db.execute("INSERT INTO archive_meta VALUES ('next_seq',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                           (str(seq),))
            else:
                seq = old["seq"]
            db.execute("""INSERT INTO receipts (key,purchased_at,seller,raw,detail,error,seq) VALUES (?,?,?,?,?,?,?)
                ON CONFLICT(key) DO UPDATE SET purchased_at=excluded.purchased_at,seller=excluded.seller,
                raw=excluded.raw,detail=excluded.detail,error=excluded.error,seq=excluded.seq""",
                       (receipt["key"], purchased_at, seller, raw, raw_detail, new_error, seq))

    def changes(self, after_seq=0, until_seq=None, limit=100):
        if (isinstance(after_seq, bool) or not isinstance(after_seq, int) or not 0 <= after_seq <= _SAFE_SEQ
                or isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100
                or (until_seq is not None and (isinstance(until_seq, bool) or not isinstance(until_seq, int)
                                                or not 0 <= until_seq <= _SAFE_SEQ))):
            raise ValueError("Invalid receipt changes window")
        with self.connect() as db:
            archive_id = db.execute("SELECT value FROM archive_meta WHERE key='archive_id'").fetchone()[0]
            current = int(db.execute("SELECT value FROM archive_meta WHERE key='next_seq'").fetchone()[0])
            window_end = current if until_seq is None else until_seq
            if window_end > current:
                raise ValueError("until_seq must not exceed the current archive revision")
            if after_seq > window_end:
                raise ValueError("after_seq must not exceed until_seq")
            rows = db.execute("""SELECT seq,key,raw,detail,error FROM receipts
                WHERE seq>? AND seq<=? ORDER BY seq LIMIT ?""", (after_seq, window_end, limit + 1)).fetchall()
        has_more = len(rows) > limit
        rows = rows[:limit]
        receipts = []
        for row in rows:
            receipt = _wire(json.loads(row["raw"]))
            fiscal_data = _wire(json.loads(row["detail"])) if row["detail"] else None
            content_hash = hashlib.sha256(_canonical({"receipt": receipt, "fiscal_data": fiscal_data}).encode("utf-8")).hexdigest()
            receipts.append({"seq": row["seq"], "key": row["key"], "content_hash": content_hash,
                            "receipt": receipt, "fiscal_data": fiscal_data, "error": row["error"]})
        next_after_seq = rows[-1]["seq"] if has_more and rows else window_end
        return {"schema_version": 1, "archive_id": archive_id, "until_seq": window_end,
                "next_after_seq": next_after_seq, "has_more": has_more, "receipts": receipts}

    def get(self, key):
        with self.connect() as db:
            row = db.execute("SELECT raw,detail,error FROM receipts WHERE key=?", (key,)).fetchone()
        if row is None:
            return {"found": False}
        return {"found": True, "receipt": json.loads(row["raw"]),
                "fiscal_data": json.loads(row["detail"]) if row["detail"] else None, "error": row["error"]}

    @staticmethod
    def filters(date_from=None, date_to=None, seller=None):
        dates(date_from, date_to)
        sql, params = " WHERE 1=1", []
        if date_from:
            sql += " AND substr(purchased_at,1,10)>=?"
            params.append(date_from)
        if date_to:
            sql += " AND substr(purchased_at,1,10)<=?"
            params.append(date_to)
        if seller:
            if len(seller) > 200:
                raise ValueError("Seller filter is too long")
            sql += " AND instr(lower(seller),lower(?))>0"
            params.append(seller)
        return sql, params

    def list(self, date_from=None, date_to=None, seller=None, limit=50, offset=0):
        if not 1 <= limit <= 200 or not 0 <= offset <= 1000000:
            raise ValueError("limit must be 1..200 and offset 0..1000000")
        where, params = self.filters(date_from, date_to, seller)
        with self.connect() as db:
            count = db.execute("SELECT COUNT(*) FROM receipts" + where, params).fetchone()[0]
            rows = db.execute("SELECT key,purchased_at,seller,raw,detail IS NOT NULL AS detailed,error FROM receipts"
                              + where + " ORDER BY purchased_at DESC,key LIMIT ? OFFSET ?", params + [limit, offset]).fetchall()
        return {"total": count, "receipts": [{"key": r["key"], "date": r["purchased_at"],
                 "seller": r["seller"], "total_sum_raw": json.loads(r["raw"]).get("totalSum"),
                 "detailed": bool(r["detailed"]), "error": r["error"]} for r in rows]}

    def spending(self, amount_divisor, date_from=None, date_to=None):
        if amount_divisor not in (1, 100):
            raise ValueError("amount_divisor must be 1 (rubles) or 100 (kopecks)")
        where, params = self.filters(date_from, date_to)
        groups, excluded = {}, 0
        with self.connect() as db:
            for row in db.execute("SELECT seller,detail FROM receipts" + where, params):
                d = json.loads(row["detail"]) if row["detail"] else {}
                # Actual payments, not totalSum: advances are not charged again at settlement.
                if d.get("operationType") not in (1, 2) or any(k not in d for k in ("cashTotalSum", "ecashTotalSum")):
                    excluded += 1
                    continue
                try:
                    amount = (Decimal(str(d["cashTotalSum"])) + Decimal(str(d["ecashTotalSum"]))) / amount_divisor
                    if not amount.is_finite() or amount < 0:
                        raise InvalidOperation
                except (InvalidOperation, TypeError, ValueError):
                    excluded += 1
                    continue
                key = row["seller"] or "Unknown"
                g = groups.setdefault(key, {"paid": Decimal(0), "returned": Decimal(0), "count": 0})
                g["returned" if d["operationType"] == 2 else "paid"] += amount
                g["count"] += 1
        rows = [{"seller": k, "paid": str(v["paid"]), "returned": str(v["returned"]),
                 "net": str(v["paid"]-v["returned"]), "receipts": v["count"]} for k,v in groups.items()]
        return {"currency": "RUB", "amount_divisor": amount_divisor,
                "basis": "cashTotalSum + ecashTotalSum; returns subtracted; unit must be verified against a real receipt",
                "excluded_receipts": excluded, "net": str(sum((v["paid"]-v["returned"] for v in groups.values()), Decimal(0))),
                "by_seller": sorted(rows, key=lambda r: Decimal(r["net"]), reverse=True)[:200]}
