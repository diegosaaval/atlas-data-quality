"""Failure Lab: a catalog of realistic production failures that can be injected live.

Each fault declares where it lands, which check is expected to catch it and how
it is remediated. Tests assert that every fault in this catalog is detected and
classified correctly, so the catalog doubles as a regression suite.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class FaultSpec:
    id: str
    name: str
    description: str
    target: str  # dataset where the fault is introduced
    detected_on: str  # dataset where the root-cause check should fire
    root_cause: str  # expected root-cause category
    remediation: str


FAULTS: tuple[FaultSpec, ...] = (
    FaultSpec("schema_drift", "Schema drift",
              "Gateway deploys v3 payload: `amount` renamed to `amount_value`, new `fee` column.",
              "src.payments.transactions", "bronze.transactions", "schema_drift",
              "Roll back producer or publish contract v3 with a column mapping."),
    FaultSpec("missing_partition", "Missing partition",
              "The 5-minute transactions partition stops arriving (upstream job stalled).",
              "src.payments.transactions", "bronze.transactions", "late_data",
              "Restart the upstream export and backfill the missing partitions."),
    FaultSpec("duplicate_load", "Duplicate load",
              "A retry replays the previous batch on top of the new one.",
              "src.payments.transactions", "bronze.transactions", "duplicates",
              "Make the loader idempotent (merge on txn_id) and purge the replayed batch."),
    FaultSpec("volume_spike", "Volume anomaly",
              "Batch size jumps ~8x — a bot, a misconfigured export or a real event.",
              "src.payments.transactions", "bronze.transactions", "volume_anomaly",
              "Confirm with the producer; if legitimate, acknowledge and re-baseline."),
    FaultSpec("null_burst", "Null burst",
              "25% of records arrive without merchant_id / account_id.",
              "src.payments.transactions", "bronze.transactions", "completeness",
              "Fix the producer mapping; quarantined rows are replayed after the fix."),
    FaultSpec("orphan_records", "Referential integrity break",
              "Transactions reference accounts that do not exist in the core.",
              "src.payments.transactions", "silver.transactions", "referential_integrity",
              "Re-sync accounts CDC, then replay quarantined transactions."),
    FaultSpec("broken_replication", "Broken replication",
              "The read replica stops applying WAL; counts and amounts diverge.",
              "src.payments.replica", "bronze.transactions", "replication",
              "Rebuild the replica slot and re-run reconciliation."),
    FaultSpec("permission_failure", "Permission failure",
              "An IAM policy change revokes read access to the accounts bucket.",
              "src.core_banking.accounts", "bronze.accounts", "access",
              "Restore the IAM role policy (least privilege) and re-run ingestion."),
    FaultSpec("late_fx", "Late FX data",
              "The market data provider stops publishing USD/COP quotes.",
              "src.fx.rates", "bronze.fx_rates", "late_data",
              "Fail over to the secondary FX provider and backfill quotes."),
    FaultSpec("fraud_burst", "Fraud pattern",
              "A card-testing attack: many high-value card-not-present transactions.",
              "src.payments.transactions", "silver.transactions", "distribution_drift",
              "Escalate to fraud ops; the fraud model consumes the refreshed features."),
)

FAULTS_BY_ID = {f.id: f for f in FAULTS}


@dataclass
class ActiveFault:
    spec: FaultSpec
    injected_tick: int
    auto_heal_tick: int | None = None
    meta: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "id": self.spec.id,
            "name": self.spec.name,
            "target": self.spec.target,
            "injected_tick": self.injected_tick,
            "auto_heal_tick": self.auto_heal_tick,
        }
