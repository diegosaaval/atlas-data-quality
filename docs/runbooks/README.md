# Runbooks

Generated from `atlas/runbooks.py` (the API serves the same content on every incident).

## Access / permission failure  `access`

Triggered in the Failure Lab by: Permission failure.

1. Check CloudTrail for recent IAM policy changes on the ingestion role.
2. Restore the last known-good policy from Terraform state (never widen to *).
3. Re-run the failed ingestion task; ATLAS backfills withheld batches automatically.
4. Add a policy-simulation test to the Terraform CI pipeline.

## Pipeline task failure  `pipeline_failure`

1. Open the task logs and identify the failing step.
2. Retry once; if it fails again, page the dataset owner.
3. Backfill the missed partitions after the fix.

## Schema drift against the data contract  `schema_drift`

Triggered in the Failure Lab by: Schema drift.

1. Diff the observed payload against the contract (see evidence).
2. Contact the producer: rollback, or publish a new contract version with a mapping.
3. Replay quarantined records once the contract or mapping is updated.
4. Keep the quality gate closed until the contract check is green.

## Late or missing data  `late_data`

Triggered in the Failure Lab by: Missing partition, Late FX data.

1. Confirm the upstream job / provider status.
2. Restart the export or fail over to the secondary provider.
3. Backfill missing partitions and verify freshness returns within SLA.
4. Notify consumers that downstream products were stale during the window.

## Replica divergence  `replication`

Triggered in the Failure Lab by: Broken replication.

1. Check replication lag and slot status on the primary.
2. Rebuild the replication slot if WAL is no longer being applied.
3. Re-run reconciliation; finance reports stay blocked until the gap is < 0.5%.

## Duplicate load  `duplicates`

Triggered in the Failure Lab by: Duplicate load.

1. Identify the replayed batch_id in bronze.
2. Silver is idempotent (merge on txn_id), so no duplicates reached gold.
3. Fix the retry policy in the loader so retries are idempotent.

## Referential integrity break  `referential_integrity`

Triggered in the Failure Lab by: Referential integrity break.

1. List orphan account_ids from quarantine.
2. Check accounts CDC lag; re-sync if accounts are missing.
3. Replay quarantined transactions after accounts are present.

## Null burst / incomplete records  `completeness`

Triggered in the Failure Lab by: Null burst.

1. Find the column(s) with the highest null rate in the evidence.
2. Check the producer's latest deployment for a mapping regression.
3. Replay quarantined rows after the fix.

## Volume anomaly  `volume_anomaly`

Triggered in the Failure Lab by: Volume anomaly.

1. Compare against business calendar (campaigns, paydays, holidays).
2. Confirm with the producer whether the volume is legitimate.
3. If legitimate, acknowledge and re-baseline; if not, purge the batch.

## Contract violations  `invalid_records`

1. Group quarantine reasons to find the dominant violation.
2. Fix at the producer, then replay quarantined rows.

## Distribution drift (possible fraud pattern)  `distribution_drift`

Triggered in the Failure Lab by: Fraud pattern.

1. Inspect gold.fraud_features for accounts with high risk_score.
2. Escalate to fraud operations with the affected account list.
3. Data keeps flowing (non-blocking): the fraud model needs fresh features.
