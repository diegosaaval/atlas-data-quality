# 0002 — SQLite as the local engine

**Status:** accepted

## Context
The project must run with one command on any laptop or CI runner, start in seconds, and be cheap to keep alive
as a public demo. Spark/Databricks would demonstrate scale but would make the demo slow, costly and fragile.

## Decision
Use SQLite (stdlib) as the warehouse. Keep every transformation as plain SQL (`warehouse.GOLD_MODELS`) and every
check as a pure Python function over metrics, so neither depends on the engine.

## Consequences
- Zero infrastructure, deterministic tests, ~3 ms per pipeline run.
- Porting path: gold models → dbt models on Databricks SQL / Athena; silver merge → Delta `MERGE INTO`;
  checks → dbt tests / a PySpark job emitting the same `CheckResult` records. See ROADMAP.md.
- Not a claim about scale. Volume here is ~100 rows per 5-minute batch.
