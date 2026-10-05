"""SQLite storage: the monitored tables (as loaded) plus ATLAS's own metadata.

* t_<table>        rows as loaded each day, tagged with _fecha_carga (last N days kept)
* loads            one row per table and day: arrival time, rows, columns received
* metrics          daily time series used for trends and outlier baselines
* check_results    every check evaluated, every day (powers the quality history heatmap)
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from typing import Any

from .tables import TABLES, TableSpec

SQL_TYPES = {"texto": "TEXT", "entero": "INTEGER", "decimal": "REAL", "fecha": "TEXT"}

META = """
CREATE TABLE loads (fecha TEXT, tabla TEXT, llegada TEXT, filas INTEGER, columnas TEXT,
                    PRIMARY KEY (fecha, tabla));
CREATE TABLE metrics (fecha TEXT, tabla TEXT, metrica TEXT, valor REAL);
CREATE INDEX ix_metrics ON metrics (tabla, metrica, fecha);
CREATE TABLE check_results (fecha TEXT, tabla TEXT, check_id TEXT, nombre TEXT, estado TEXT,
                            valor REAL, filas_afectadas INTEGER);
CREATE INDEX ix_checks ON check_results (tabla, fecha);
"""


class Store:
    def __init__(self, path: str = ":memory:") -> None:
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        with self.lock:
            for (name,) in self.conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
                self.conn.execute(f"DROP TABLE IF EXISTS {name}")
            self.conn.executescript(META)
            for spec in TABLES:
                cols = ", ".join(f"{c.name} {SQL_TYPES[c.type]}" for c in spec.columns)
                self.conn.execute(f"CREATE TABLE t_{spec.name} ({cols}, _fecha_carga TEXT)")
                self.conn.execute(f"CREATE INDEX ix_{spec.name} ON t_{spec.name} (_fecha_carga)")

    def close(self) -> None:
        with self.lock:
            self.conn.close()

    # ------------------------------------------------------------------ reads
    def query(self, sql: str, params: dict[str, Any] | tuple = ()) -> list[dict[str, Any]]:
        with self.lock:
            return [dict(r) for r in self.conn.execute(sql, params)]

    def scalar(self, sql: str, params: dict[str, Any] | tuple = ()) -> Any:
        with self.lock:
            row = self.conn.execute(sql, params).fetchone()
            return row[0] if row else None

    def query_readonly(self, sql: str, params: dict[str, Any] | tuple = (), timeout: float = 0.5,
                       max_rows: int = 1000) -> list[dict[str, Any]]:
        """Run untrusted (user rule) SQL: writes disabled, time-boxed and with a row cap.

        A recursive CTE or a huge cross join is interrupted after `timeout` seconds instead of
        freezing the monitor for everybody.
        """
        deadline = time.monotonic() + timeout
        with self.lock:
            self.conn.execute("PRAGMA query_only = ON")
            self.conn.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 5_000)
            try:
                return [dict(r) for r in self.conn.execute(sql, params).fetchmany(max_rows)]
            except sqlite3.OperationalError as exc:
                if "interrupted" in str(exc):
                    raise TimeoutError(f"la consulta superó {timeout:g} s y se canceló") from exc
                raise
            finally:
                self.conn.set_progress_handler(None, 0)
                self.conn.execute("PRAGMA query_only = OFF")

    # ----------------------------------------------------------------- writes
    def insert_load(self, spec: TableSpec, day: str, arrived: str, rows: list[dict], columns: list[str]) -> None:
        names = spec.column_names
        with self.lock:
            self.conn.executemany(
                f"INSERT INTO t_{spec.name} ({', '.join(names)}, _fecha_carga) "
                f"VALUES ({', '.join('?' * (len(names) + 1))})",
                [tuple(r.get(n) for n in names) + (day,) for r in rows],
            )
            self.conn.execute("INSERT OR REPLACE INTO loads VALUES (?, ?, ?, ?, ?)",
                              (day, spec.name, arrived, len(rows), json.dumps(columns)))

    def put_metric(self, day: str, table: str, metric: str, value: float | None) -> None:
        if value is None:
            return
        with self.lock:
            self.conn.execute("INSERT INTO metrics VALUES (?, ?, ?, ?)", (day, table, metric, value))

    def metric_history(self, table: str, metric: str, before: str, limit: int = 60) -> list[tuple[str, float]]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT fecha, valor FROM metrics WHERE tabla = ? AND metrica = ? AND fecha < ? "
                "ORDER BY fecha DESC LIMIT ?", (table, metric, before, limit)).fetchall()
        return [(r[0], r[1]) for r in reversed(rows)]

    def record_checks(self, day: str, results: list[Any]) -> None:
        with self.lock:
            self.conn.executemany(
                "INSERT INTO check_results VALUES (?, ?, ?, ?, ?, ?, ?)",
                [(day, r.table, r.check_id, r.name, r.status, r.value, r.failing_rows) for r in results],
            )

    def prune_rows(self, keep_from: str) -> None:
        with self.lock:
            for spec in TABLES:
                self.conn.execute(f"DELETE FROM t_{spec.name} WHERE _fecha_carga < ?", (keep_from,))

    def prune_history(self, keep_from: str) -> None:
        """Metrics and check results older than `keep_from` are dropped (keeps the demo bounded)."""
        with self.lock:
            self.conn.execute("DELETE FROM metrics WHERE fecha < ?", (keep_from,))
            self.conn.execute("DELETE FROM check_results WHERE fecha < ?", (keep_from,))
