"""Root-cause taxonomy and runbooks.

Classification is rule-based on purpose: it is explainable, testable and the
AI copilot only *explains* it, it never overrides it.
"""

from __future__ import annotations

from .checks import CheckResult

# Most specific / most upstream evidence first.
PRIORITY = ("pipeline", "schema", "freshness", "consistency", "uniqueness", "integrity",
            "completeness", "volume", "validity", "distribution")

CATEGORY_BY_DIMENSION = {
    "pipeline": "pipeline_failure", "schema": "schema_drift", "freshness": "late_data",
    "consistency": "replication", "uniqueness": "duplicates", "integrity": "referential_integrity",
    "completeness": "completeness", "volume": "volume_anomaly", "validity": "invalid_records",
    "distribution": "distribution_drift",
}

RUNBOOKS: dict[str, dict] = {
    "access": {
        "title": "Access / permission failure",
        "steps": [
            "Check CloudTrail for recent IAM policy changes on the ingestion role.",
            "Restore the last known-good policy from Terraform state (never widen to *).",
            "Re-run the failed ingestion task; ATLAS backfills withheld batches automatically.",
            "Add a policy-simulation test to the Terraform CI pipeline.",
        ],
    },
    "pipeline_failure": {
        "title": "Pipeline task failure",
        "steps": [
            "Open the task logs and identify the failing step.",
            "Retry once; if it fails again, page the dataset owner.",
            "Backfill the missed partitions after the fix.",
        ],
    },
    "schema_drift": {
        "title": "Schema drift against the data contract",
        "steps": [
            "Diff the observed payload against the contract (see evidence).",
            "Contact the producer: rollback, or publish a new contract version with a mapping.",
            "Replay quarantined records once the contract or mapping is updated.",
            "Keep the quality gate closed until the contract check is green.",
        ],
    },
    "late_data": {
        "title": "Late or missing data",
        "steps": [
            "Confirm the upstream job / provider status.",
            "Restart the export or fail over to the secondary provider.",
            "Backfill missing partitions and verify freshness returns within SLA.",
            "Notify consumers that downstream products were stale during the window.",
        ],
    },
    "replication": {
        "title": "Replica divergence",
        "steps": [
            "Check replication lag and slot status on the primary.",
            "Rebuild the replication slot if WAL is no longer being applied.",
            "Re-run reconciliation; finance reports stay blocked until the gap is < 0.5%.",
        ],
    },
    "duplicates": {
        "title": "Duplicate load",
        "steps": [
            "Identify the replayed batch_id in bronze.",
            "Silver is idempotent (merge on txn_id), so no duplicates reached gold.",
            "Fix the retry policy in the loader so retries are idempotent.",
        ],
    },
    "referential_integrity": {
        "title": "Referential integrity break",
        "steps": [
            "List orphan account_ids from quarantine.",
            "Check accounts CDC lag; re-sync if accounts are missing.",
            "Replay quarantined transactions after accounts are present.",
        ],
    },
    "completeness": {
        "title": "Null burst / incomplete records",
        "steps": [
            "Find the column(s) with the highest null rate in the evidence.",
            "Check the producer's latest deployment for a mapping regression.",
            "Replay quarantined rows after the fix.",
        ],
    },
    "volume_anomaly": {
        "title": "Volume anomaly",
        "steps": [
            "Compare against business calendar (campaigns, paydays, holidays).",
            "Confirm with the producer whether the volume is legitimate.",
            "If legitimate, acknowledge and re-baseline; if not, purge the batch.",
        ],
    },
    "invalid_records": {
        "title": "Contract violations",
        "steps": [
            "Group quarantine reasons to find the dominant violation.",
            "Fix at the producer, then replay quarantined rows.",
        ],
    },
    "distribution_drift": {
        "title": "Distribution drift (possible fraud pattern)",
        "steps": [
            "Inspect gold.fraud_features for accounts with high risk_score.",
            "Escalate to fraud operations with the affected account list.",
            "Data keeps flowing (non-blocking): the fraud model needs fresh features.",
        ],
    },
}

LABELS = {k: v["title"] for k, v in RUNBOOKS.items()}


def classify(check: CheckResult) -> str:
    category = CATEGORY_BY_DIMENSION[check.dimension]
    if category == "pipeline_failure" and "AccessDenied" in check.message:
        return "access"
    return category


def primary(checks: list[CheckResult]) -> CheckResult:
    return min(checks, key=lambda c: PRIORITY.index(c.dimension))
