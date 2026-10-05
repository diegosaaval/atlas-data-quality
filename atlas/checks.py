"""Deterministic reliability checks.

Checks are pure functions: metrics in, CheckResult out. They never touch the
warehouse, which keeps them trivial to unit test and to port to Spark/dbt.
"""

from __future__ import annotations

import statistics
from collections.abc import Iterable
from dataclasses import asdict, dataclass

from .contracts import Contract

PASS, WARN, FAIL = "pass", "warn", "fail"

# Dimension -> score penalty when the check fails (warn = 40% of it).
WEIGHTS = {
    "pipeline": 35, "freshness": 30, "schema": 25, "validity": 20, "integrity": 20,
    "uniqueness": 20, "consistency": 20, "volume": 15, "completeness": 15, "distribution": 10,
}

# Checks whose failure stops data from flowing to gold (quality gate).
BLOCKING = {"schema", "validity", "integrity", "uniqueness", "consistency", "volume", "completeness"}


@dataclass(frozen=True)
class CheckResult:
    dataset: str
    name: str
    dimension: str
    status: str
    value: float | None
    threshold: str
    message: str

    @property
    def blocking(self) -> bool:
        return self.status == FAIL and self.dimension in BLOCKING

    def to_dict(self) -> dict:
        return {**asdict(self), "blocking": self.blocking}


def _status(value: float, warn: float, fail: float) -> str:
    if value >= fail:
        return FAIL
    if value >= warn:
        return WARN
    return PASS


def freshness(dataset: str, age_minutes: float | None, sla_minutes: int, interval: int) -> CheckResult:
    if age_minutes is None:
        return CheckResult(dataset, "freshness", "freshness", WARN, None, f"≤ {sla_minutes} min",
                           "No data loaded yet")
    status = FAIL if age_minutes > sla_minutes else WARN if age_minutes > interval * 2 else PASS
    return CheckResult(dataset, "freshness", "freshness", status, round(age_minutes, 1),
                       f"≤ {sla_minutes} min", f"Data is {age_minutes:.0f} min old (SLA {sla_minutes} min)")


def pipeline(dataset: str, error: str | None) -> CheckResult:
    if error:
        return CheckResult(dataset, "pipeline_run", "pipeline", FAIL, 1, "no task errors", error)
    return CheckResult(dataset, "pipeline_run", "pipeline", PASS, 0, "no task errors", "Task succeeded")


def schema(dataset: str, contract: Contract, observed: set[str]) -> CheckResult:
    diff = contract.schema_diff(observed)
    missing_required = [c for c in diff["missing"] if c in contract.required_columns]
    if missing_required:
        status = FAIL
    elif diff["unexpected"] or diff["missing"]:
        status = WARN
    else:
        status = PASS
    parts = []
    if diff["missing"]:
        parts.append(f"missing {diff['missing']}")
    if diff["unexpected"]:
        parts.append(f"unexpected {diff['unexpected']}")
    message = f"Contract v{contract.version}: " + ("; ".join(parts) if parts else "schema matches")
    return CheckResult(dataset, "schema_contract", "schema", status,
                       float(len(diff["missing"]) + len(diff["unexpected"])), "0 differences", message)


def volume(dataset: str, rows: int, history: Iterable[int]) -> CheckResult:
    history = list(history)
    if len(history) < 6:
        return CheckResult(dataset, "volume", "volume", PASS, float(rows), "warming up",
                           f"{rows} rows (baseline warming up)")
    baseline = statistics.median(history)
    ratio = rows / baseline if baseline else 1.0
    if ratio >= 3 or ratio <= 0.25:
        status = FAIL
    elif ratio >= 2 or ratio <= 0.4:
        status = WARN
    else:
        status = PASS
    return CheckResult(dataset, "volume", "volume", status, round(ratio, 2), "0.4x – 2x baseline",
                       f"{rows} rows vs baseline {baseline:.0f} ({ratio:.1f}x)")


def duplicates(dataset: str, duplicate_rows: int, total: int) -> CheckResult:
    rate = duplicate_rows / total if total else 0.0
    status = _status(rate, 0.001, 0.01)
    return CheckResult(dataset, "duplicates", "uniqueness", status, round(rate, 4), "< 1%",
                       f"{duplicate_rows} duplicate txn_id of {total} ({rate:.1%})")


def completeness(dataset: str, null_counts: dict[str, int], total: int) -> CheckResult:
    worst_col, worst = max(null_counts.items(), key=lambda kv: kv[1], default=("-", 0))
    rate = worst / total if total else 0.0
    status = _status(rate, 0.005, 0.02)
    return CheckResult(dataset, "null_rate", "completeness", status, round(rate, 4), "< 2% per required column",
                       f"Worst column {worst_col}: {worst} nulls ({rate:.1%})")


def reconciliation(dataset: str, count: float, amount: float, replica: dict[str, float]) -> CheckResult:
    count_gap = abs(count - replica["count"]) / count if count else 0.0
    amount_gap = abs(amount - replica["amount"]) / amount if amount else 0.0
    gap = max(count_gap, amount_gap)
    status = _status(gap, 0.001, 0.005)
    return CheckResult(dataset, "replica_reconciliation", "consistency", status, round(gap, 4), "< 0.5% gap",
                       f"Primary vs replica: count gap {count_gap:.1%}, amount gap {amount_gap:.1%}")


def validity(dataset: str, quarantined: int, total: int) -> CheckResult:
    rate = quarantined / total if total else 0.0
    status = _status(rate, 0.01, 0.05)
    return CheckResult(dataset, "contract_validity", "validity", status, round(rate, 4), "< 5% quarantined",
                       f"{quarantined} of {total} records quarantined ({rate:.1%})")


def integrity(dataset: str, orphans: int, total: int) -> CheckResult:
    rate = orphans / total if total else 0.0
    status = _status(rate, 0.003, 0.01)
    return CheckResult(dataset, "referential_integrity", "integrity", status, round(rate, 4), "< 1% orphans",
                       f"{orphans} transactions reference unknown accounts ({rate:.1%})")


def distribution(dataset: str, mean_amount: float, history: Iterable[float]) -> CheckResult:
    history = list(history)
    if len(history) < 6 or mean_amount <= 0:
        return CheckResult(dataset, "amount_distribution", "distribution", PASS, None, "warming up",
                           "Baseline warming up")
    baseline = statistics.median(history)
    ratio = mean_amount / baseline
    status = FAIL if ratio >= 2.2 else WARN if ratio >= 1.7 else PASS
    return CheckResult(dataset, "amount_distribution", "distribution", status, round(ratio, 2), "< 1.7x baseline",
                       f"Mean amount COP {mean_amount:,.0f} vs baseline {baseline:,.0f} ({ratio:.1f}x)")


def dataset_score(results: list[CheckResult]) -> float:
    score = 100.0
    for r in results:
        weight = WEIGHTS[r.dimension]
        if r.status == FAIL:
            score -= weight
        elif r.status == WARN:
            score -= weight * 0.4
    return max(0.0, score)
