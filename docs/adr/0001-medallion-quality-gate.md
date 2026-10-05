# 0001 — Medallion layers with a lineage-aware quality gate

**Status:** accepted

## Context
Financial consumers (ledger, regulatory report, fraud model) prefer *stale but correct* over *fresh but wrong*.
A pipeline that publishes whatever arrives turns every upstream problem into a downstream incident.

## Decision
- Bronze is raw and append-only (replayable). Silver validates against data contracts, quarantines bad rows,
  merges idempotently on the primary key and enriches. Gold models are SQL over silver.
- Before each gold build, a **quality gate** checks every *blocking* failure on the model's ancestors (from the
  lineage graph). If any exists, the build is skipped and the last good version is kept.
- Blocking dimensions: schema, validity, completeness, uniqueness, integrity, consistency, volume.
  Non-blocking: freshness (nothing to block) and distribution (the fraud model needs fresh data most during an attack).
- Volume failures block. A real 8x spike may be legitimate (Black Friday), but confirming takes minutes and
  publishing duplicated money to a ledger is worse. The runbook includes "acknowledge and re-baseline".

## Consequences
- Bad data never reaches gold; consumers see staleness, which freshness checks surface explicitly.
- Freshness propagates: a derived dataset's `data_as_of` is the minimum of its inputs.
- Gate decisions are per model, so an issue on accounts does not block models that do not read accounts.
