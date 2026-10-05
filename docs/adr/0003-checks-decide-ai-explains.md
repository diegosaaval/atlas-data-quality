# 0003 — Deterministic checks decide, AI explains

**Status:** accepted

## Context
LLMs are good at summarising evidence and drafting stakeholder updates, and bad at being an auditable source of
truth for whether regulated data is correct.

## Decision
- Pass/fail, gating, root-cause classification and severity are deterministic and unit-tested.
- The copilot receives the evidence ATLAS already gathered (checks, lineage, quarantine reasons, task errors,
  history, runbook) and returns a structured briefing (JSON schema). The system prompt forbids contradicting checks.
- Default backend is a rules engine. Claude is used only when `ANTHROPIC_API_KEY` is set, with low effort,
  structured output and server-side fallbacks; any error falls back to rules.

## Consequences
- The platform behaves identically with or without the LLM. Incident response never waits on a model.
- The AI adds value where humans spend time (writing the update, connecting evidence), not where auditors look.
