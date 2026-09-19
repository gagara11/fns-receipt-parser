"""Persistent raw archive. All user filters are bound SQL parameters."""

import json
import sqlite3
from contextlib import contextmanager
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path


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
            """)

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
            db.execute("""INSERT INTO receipts VALUES (?,?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET
                raw=excluded.raw, detail=COALESCE(excluded.detail,receipts.detail),
                purchased_at=CASE WHEN excluded.detail IS NOT NULL THEN excluded.purchased_at ELSE receipts.purchased_at END,
                seller=CASE WHEN excluded.detail IS NOT NULL THEN excluded.seller ELSE receipts.seller END,
                error=excluded.error""", (receipt["key"], d.get("dateTime") or receipt.get("createdDate"),
                    d.get("user") or receipt.get("kktOwner", ""), json.dumps(receipt, ensure_ascii=False),
                    json.dumps(detail, ensure_ascii=False) if detail is not None else None, error))

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
