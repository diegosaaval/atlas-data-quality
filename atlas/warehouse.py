"""Medallion warehouse on SQLite.

SQLite keeps the demo dependency-free and lets every transformation be plain
SQL. The same models map 1:1 to Delta tables on Databricks or Iceberg on Athena
(see docs/adr/0002-sqlite-as-local-engine.md).
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterable
from typing import Any

SCHEMA = """
CREATE TABLE bronze_accounts     (batch_id TEXT, tick INTEGER, ingested_at TEXT, payload TEXT);
CREATE TABLE bronze_transactions (batch_id TEXT, tick INTEGER, ingested_at TEXT, payload TEXT, is_backfill INTEGER);
CREATE TABLE bronze_fx_rates     (batch_id TEXT, tick INTEGER, ingested_at TEXT, payload TEXT);

CREATE TABLE silver_accounts (
    account_id TEXT PRIMARY KEY, segment TEXT, risk_tier TEXT, status TEXT,
    opened_at TEXT, holder_name TEXT, tick INTEGER
);
CREATE TABLE silver_transactions (
    txn_id TEXT PRIMARY KEY, account_id TEXT, merchant_id TEXT, merchant_category TEXT,
    channel TEXT, amount REAL, currency TEXT, amount_cop REAL, event_time TEXT,
    status TEXT, tick INTEGER
);
CREATE INDEX ix_silver_txn_tick ON silver_transactions(tick);
CREATE INDEX ix_silver_txn_account ON silver_transactions(account_id);
CREATE TABLE silver_fx_rates (pair TEXT, rate REAL, as_of TEXT, tick INTEGER);

CREATE TABLE quarantine (dataset TEXT, batch_id TEXT, tick INTEGER, payload TEXT, reasons TEXT);

CREATE TABLE gold_daily_ledger (
    day TEXT, merchant_category TEXT, currency TEXT,
    txn_count INTEGER, gross_amount_cop REAL, approved_amount_cop REAL
);
CREATE TABLE gold_risk_exposure (
    segment TEXT, risk_tier TEXT, txn_count INTEGER, exposure_cop REAL, high_value_count INTEGER
);
CREATE TABLE gold_fraud_features (
    account_id TEXT, txn_count_1h INTEGER, amount_sum_1h REAL, max_amount_1h REAL,
    distinct_merchants_1h INTEGER, cnp_ratio REAL, risk_score REAL
);
CREATE TABLE gold_payment_ops (
    window_tick INTEGER, txn_count INTEGER, approval_rate REAL, decline_rate REAL, reversal_rate REAL
);

CREATE TABLE check_results (
    tick INTEGER, dataset TEXT, check_name TEXT, status TEXT, value REAL, threshold TEXT, message TEXT
);
CREATE INDEX ix_checks ON check_results(dataset, tick);
"""

# dbt-style models: one SELECT per gold table, full refresh over the retained window.
# {since_tick} is bound per run for windowed models.
GOLD_MODELS: dict[str, str] = {
    "gold.daily_ledger": """
        SELECT substr(event_time, 1, 10)             AS day,
               merchant_category,
               currency,
               COUNT(*)                              AS txn_count,
               ROUND(SUM(amount_cop), 2)             AS gross_amount_cop,
               ROUND(SUM(CASE WHEN status = 'approved' THEN amount_cop ELSE 0 END), 2)
                                                     AS approved_amount_cop
        FROM silver_transactions
        GROUP BY 1, 2, 3
    """,
    "gold.risk_exposure": """
        SELECT a.segment,
               a.risk_tier,
               COUNT(*)                              AS txn_count,
               ROUND(SUM(t.amount_cop), 2)           AS exposure_cop,
               SUM(CASE WHEN t.amount_cop > 1000000 THEN 1 ELSE 0 END) AS high_value_count
        FROM silver_transactions t
        JOIN silver_accounts a USING (account_id)
        WHERE t.status = 'approved'
        GROUP BY 1, 2
    """,
    "gold.fraud_features": """
        WITH w AS (
            SELECT * FROM silver_transactions WHERE tick >= :since_tick
        ), f AS (
            SELECT account_id,
                   COUNT(*)                          AS txn_count_1h,
                   ROUND(SUM(amount_cop), 2)         AS amount_sum_1h,
                   ROUND(MAX(amount_cop), 2)         AS max_amount_1h,
                   COUNT(DISTINCT merchant_id)       AS distinct_merchants_1h,
                   ROUND(AVG(CASE WHEN channel = 'card_not_present' THEN 1.0 ELSE 0 END), 3)
                                                     AS cnp_ratio
            FROM w GROUP BY account_id
        )
        SELECT *,
               ROUND(MIN(100,
                   35 * cnp_ratio
                 + 25 * MIN(1.0, max_amount_1h / 1500000.0)
                 + 20 * MIN(1.0, txn_count_1h / 8.0)
                 + 20 * MIN(1.0, distinct_merchants_1h / 6.0)), 1) AS risk_score
        FROM f
    """,
    "gold.payment_ops": """
        SELECT tick                                  AS window_tick,
               COUNT(*)                              AS txn_count,
               ROUND(AVG(status = 'approved'), 4)    AS approval_rate,
               ROUND(AVG(status = 'declined'), 4)    AS decline_rate,
               ROUND(AVG(status = 'reversed'), 4)    AS reversal_rate
        FROM silver_transactions
        WHERE tick >= :since_tick
        GROUP BY tick
    """,
}

GOLD_TABLE = {model: model.replace(".", "_") for model in GOLD_MODELS}


class Warehouse:
    def __init__(self, path: str = ":memory:") -> None:
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        with self.lock:
            tables = [r[0] for r in self.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
            for table in tables:  # each run of the simulation starts from a clean slate
                self.conn.execute(f"DROP TABLE IF EXISTS {table}")
            self.conn.executescript(SCHEMA)

    # ----------------------------------------------------------------- generic
    def query(self, sql: str, params: dict[str, Any] | tuple = ()) -> list[dict[str, Any]]:
        with self.lock:
            return [dict(r) for r in self.conn.execute(sql, params)]

    def scalar(self, sql: str, params: dict[str, Any] | tuple = ()) -> Any:
        with self.lock:
            row = self.conn.execute(sql, params).fetchone()
            return row[0] if row else None

    # ------------------------------------------------------------------ bronze
    def load_bronze(self, table: str, batch_id: str, tick: int, ingested_at: str,
                    records: Iterable[dict[str, Any]], is_backfill: bool = False) -> int:
        rows = [(batch_id, tick, ingested_at, json.dumps(r)) for r in records]
        with self.lock:
            if table == "bronze_transactions":
                self.conn.executemany(
                    "INSERT INTO bronze_transactions VALUES (?, ?, ?, ?, ?)",
                    [(*r, int(is_backfill)) for r in rows],
                )
            else:
                self.conn.executemany(f"INSERT INTO {table} VALUES (?, ?, ?, ?)", rows)
        return len(rows)

    # ------------------------------------------------------------------ silver
    def upsert_accounts(self, records: list[dict[str, Any]], tick: int) -> int:
        with self.lock:
            self.conn.executemany(
                """INSERT INTO silver_accounts VALUES (:account_id, :segment, :risk_tier, :status,
                   :opened_at, :holder_name, :tick)
                   ON CONFLICT(account_id) DO UPDATE SET status = excluded.status, tick = excluded.tick""",
                [{**r, "tick": tick} for r in records],
            )
        return len(records)

    def insert_transactions(self, records: list[dict[str, Any]], tick: int) -> int:
        """Idempotent insert: rows whose txn_id already exists are ignored."""
        with self.lock:
            before = self.conn.total_changes
            self.conn.executemany(
                """INSERT OR IGNORE INTO silver_transactions VALUES (:txn_id, :account_id, :merchant_id,
                   :merchant_category, :channel, :amount, :currency, :amount_cop, :event_time,
                   :status, :tick)""",
                [{**r, "tick": tick} for r in records],
            )
            return self.conn.total_changes - before

    def insert_fx(self, records: list[dict[str, Any]], tick: int) -> None:
        with self.lock:
            self.conn.executemany(
                "INSERT INTO silver_fx_rates VALUES (:pair, :rate, :as_of, :tick)",
                [{**r, "tick": tick} for r in records],
            )

    def latest_fx(self) -> float | None:
        return self.scalar("SELECT rate FROM silver_fx_rates ORDER BY as_of DESC LIMIT 1")

    def existing_txn_ids(self, ids: list[str]) -> set[str]:
        if not ids:
            return set()
        with self.lock:
            found: set[str] = set()
            for i in range(0, len(ids), 500):
                chunk = ids[i:i + 500]
                marks = ",".join("?" * len(chunk))
                found.update(r[0] for r in self.conn.execute(
                    f"SELECT txn_id FROM silver_transactions WHERE txn_id IN ({marks})", chunk))
            return found

    def known_accounts(self, ids: set[str]) -> set[str]:
        if not ids:
            return set()
        with self.lock:
            marks = ",".join("?" * len(ids))
            return {r[0] for r in self.conn.execute(
                f"SELECT account_id FROM silver_accounts WHERE account_id IN ({marks})", list(ids))}

    def quarantine(self, dataset: str, batch_id: str, tick: int, rows: list[tuple[dict, list[str]]]) -> None:
        with self.lock:
            self.conn.executemany(
                "INSERT INTO quarantine VALUES (?, ?, ?, ?, ?)",
                [(dataset, batch_id, tick, json.dumps(r), json.dumps(reasons)) for r, reasons in rows],
            )

    # -------------------------------------------------------------------- gold
    def build_gold(self, model: str, since_tick: int) -> int:
        table = GOLD_TABLE[model]
        with self.lock:
            self.conn.execute(f"DELETE FROM {table}")
            sql = GOLD_MODELS[model]
            params = {"since_tick": since_tick} if ":since_tick" in sql else {}
            self.conn.execute(f"INSERT INTO {table} {sql}", params)
            return self.scalar(f"SELECT COUNT(*) FROM {table}")

    # ---------------------------------------------------------------- metadata
    def record_checks(self, tick: int, results: list[Any]) -> None:
        with self.lock:
            self.conn.executemany(
                "INSERT INTO check_results VALUES (?, ?, ?, ?, ?, ?, ?)",
                [(tick, r.dataset, r.name, r.status, r.value, r.threshold, r.message) for r in results],
            )

    def prune(self, before_tick: int) -> None:
        with self.lock:
            for table in ("bronze_accounts", "bronze_transactions", "bronze_fx_rates",
                          "silver_transactions", "silver_fx_rates", "quarantine", "check_results"):
                self.conn.execute(f"DELETE FROM {table} WHERE tick < ?", (before_tick,))

    def table_counts(self) -> dict[str, int]:
        with self.lock:
            names = [r[0] for r in self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
            return {n: self.conn.execute(f"SELECT COUNT(*) FROM {n}").fetchone()[0] for n in names}
