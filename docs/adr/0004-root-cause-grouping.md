# 0004 — Group incidents by root cause using lineage

**Status:** accepted

## Context
One upstream problem makes many downstream checks fail (freshness on every gold table, quarantine on silver...).
Paging for each one creates alert fatigue and hides the cause.

## Decision
- A failing check is a **root** if none of its dataset's ancestors has a failing check in the same run;
  otherwise it is a **symptom** attached to the open incident(s) upstream.
- One open incident per root dataset; the primary check is chosen by a fixed priority
  (pipeline > schema > freshness > consistency > uniqueness > integrity > completeness > volume > validity > distribution).
- Resolution requires two consecutive green runs on the root dataset.

## Consequences
- A permission failure on accounts produces one SEV1 incident with grouped symptoms (stale silver, orphan
  transactions, held gold) instead of eight alerts.
- Two independent faults on unrelated branches still produce two incidents.
