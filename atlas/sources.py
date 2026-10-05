"""Synthetic financial sources: accounts, transactions, replica stats and FX rates.

Data is deterministic for a given seed and follows a daily seasonality curve so
volume checks have to cope with realistic variation, not a flat line.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

CATEGORIES = ("grocery", "fuel", "travel", "electronics", "transfer", "utilities", "restaurants")
CHANNELS = ("app", "pse", "card_present", "card_not_present")
FIRST = ("Ana", "Luis", "Camila", "Andrés", "Valentina", "Santiago", "María", "Juan", "Laura", "Diego")
LAST = ("Gómez", "Restrepo", "Rodríguez", "Martínez", "López", "Ramírez", "Torres", "Vélez", "Ospina")


@dataclass
class Batch:
    dataset: str
    records: list[dict[str, Any]]
    is_backfill: bool = False


@dataclass
class SourceOutput:
    accounts: Batch | None
    transactions: list[Batch]
    fx: Batch | None
    replica: dict[str, float]  # count / amount as seen by the read replica
    errors: dict[str, str] = field(default_factory=dict)


class SyntheticSources:
    BASE_TXN_PER_TICK = 90

    def __init__(self, seed: int, start: datetime) -> None:
        self.rng = random.Random(seed)
        self.start = start
        self.account_ids: list[str] = []
        self._next_account = 1
        self._fx_rate = 4050.0
        self._last_txn_batch: list[dict[str, Any]] = []
        self._withheld_txn: list[list[dict[str, Any]]] = []
        self._withheld_fx: list[dict[str, Any]] = []
        self._withheld_accounts: list[dict[str, Any]] = []
        self._initial_accounts = [self._new_account(start - timedelta(days=30)) for _ in range(250)]

    # ------------------------------------------------------------------ helpers
    def _new_account(self, when: datetime) -> dict[str, Any]:
        account_id = f"ACC-{self._next_account:06d}"
        self._next_account += 1
        self.account_ids.append(account_id)
        return {
            "account_id": account_id,
            "segment": self.rng.choices(("retail", "sme", "corporate"), (80, 15, 5))[0],
            "risk_tier": self.rng.choices(("low", "medium", "high"), (70, 25, 5))[0],
            "status": "active",
            "opened_at": when.isoformat(),
            "holder_name": f"{self.rng.choice(FIRST)} {self.rng.choice(LAST)}",
        }

    @staticmethod
    def seasonality(when: datetime) -> float:
        """Daily curve: quiet at 3am, peak around 1pm and 7pm."""
        h = when.hour + when.minute / 60
        day = 0.55 + 0.45 * math.sin((h - 7) / 24 * 2 * math.pi)
        evening = 0.25 * math.exp(-((h - 19) ** 2) / 4)
        return max(0.35, day + evening)

    def _transaction(self, tick: int, i: int, when: datetime) -> dict[str, Any]:
        rng = self.rng
        currency = "USD" if rng.random() < 0.08 else "COP"
        amount = rng.lognormvariate(math.log(85_000), 0.7)
        if currency == "USD":
            amount = amount / self._fx_rate
        return {
            "txn_id": f"TXN-{tick:06d}-{i:04d}",
            "account_id": rng.choice(self.account_ids),
            "merchant_id": f"MER-{rng.randint(1, 400):04d}",
            "merchant_category": rng.choice(CATEGORIES),
            "channel": rng.choices(CHANNELS, (40, 15, 30, 15))[0],
            "amount": round(amount, 2),
            "currency": currency,
            "event_time": (when - timedelta(seconds=rng.randint(0, 299))).isoformat(),
            "status": rng.choices(("approved", "declined", "reversed"), (92, 7, 1))[0],
        }

    # --------------------------------------------------------------- generation
    def emit(self, tick: int, when: datetime, faults: set[str]) -> SourceOutput:
        rng = self.rng
        errors: dict[str, str] = {}

        # Accounts: initial snapshot on the first tick, then a trickle of new accounts.
        new_accounts = self._initial_accounts if tick == 0 else [
            self._new_account(when) for _ in range(rng.choices((0, 1, 2), (50, 35, 15))[0])
        ]
        if "permission_failure" in faults:
            self._withheld_accounts.extend(new_accounts)
            accounts = None
            errors["bronze.accounts"] = (
                "AccessDenied: s3:GetObject on s3://atlas-landing/core/accounts/ "
                "(role atlas-ingest-role)"
            )
        else:
            accounts = Batch("bronze.accounts", self._withheld_accounts + new_accounts)
            self._withheld_accounts = []

        # Transactions
        n = max(5, int(self.BASE_TXN_PER_TICK * self.seasonality(when) * rng.gauss(1, 0.08)))
        if "volume_spike" in faults:
            n *= 8
        records = [self._transaction(tick, i, when) for i in range(n)]

        if "null_burst" in faults:
            for r in records:
                if rng.random() < 0.25:
                    r[rng.choice(("merchant_id", "account_id"))] = None
        if "orphan_records" in faults:
            for r in records:
                if rng.random() < 0.15:
                    r["account_id"] = f"ACC-9{rng.randint(0, 99999):05d}"
        if "fraud_burst" in faults:
            for r in records:
                if rng.random() < 0.35:
                    r["channel"] = "card_not_present"
                    r["merchant_category"] = "electronics"
                    r["amount"] = round(r["amount"] * 8, 2)
        if "schema_drift" in faults:
            for r in records:
                r["amount_value"] = r.pop("amount")
                r["fee"] = round(r["amount_value"] * 0.012, 2)

        replica = {"count": float(len(records)), "amount": sum(_amount(r) for r in records)}
        if "broken_replication" in faults:
            replica = {"count": replica["count"] * 0.7, "amount": replica["amount"] * 0.66}

        txn_batches: list[Batch] = []
        if "missing_partition" in faults:
            self._withheld_txn.append(records)
        else:
            payload = list(records)
            if "duplicate_load" in faults and self._last_txn_batch:
                payload += [dict(r) for r in self._last_txn_batch]
            txn_batches.append(Batch("bronze.transactions", payload))
            for withheld in self._withheld_txn:
                txn_batches.append(Batch("bronze.transactions", withheld, is_backfill=True))
            self._withheld_txn = []
            self._last_txn_batch = records

        # FX: random walk
        self._fx_rate *= math.exp(rng.gauss(0, 0.0008))
        quote = {"pair": "USD/COP", "rate": round(self._fx_rate, 2), "as_of": when.isoformat()}
        if "late_fx" in faults:
            self._withheld_fx.append(quote)
            fx = None
        else:
            fx = Batch("bronze.fx_rates", self._withheld_fx + [quote])
            self._withheld_fx = []

        return SourceOutput(accounts=accounts, transactions=txn_batches, fx=fx, replica=replica, errors=errors)


def _amount(record: dict[str, Any]) -> float:
    value = record.get("amount", record.get("amount_value", 0.0))
    return float(value or 0.0)
