"""AI Incident Copilot.

Design rule: deterministic checks decide whether data is good; the copilot only
explains. It receives the evidence ATLAS already collected (checks, lineage,
quarantine reasons, runbook, history) and turns it into a briefing.

Two backends:
* rules  (default) — template-based, offline, instant, fully testable.
* claude (opt-in)  — used when ANTHROPIC_API_KEY is set; falls back to rules on any error.
"""

from __future__ import annotations

import json
import logging
import os
from collections import Counter
from typing import Any

from . import catalog
from .engine import Engine

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are the incident copilot of ATLAS, a data reliability platform for a bank.
You receive evidence that deterministic checks already collected. Do not contradict the check
results or the classified root cause; explain them. Be concise and specific: cite check names,
values and dataset ids from the evidence. Never invent datasets, numbers or owners."""

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "probable_root_cause": {"type": "string"},
        "evidence": {"type": "array", "items": {"type": "string"}},
        "affected_assets": {"type": "array", "items": {"type": "string"}},
        "remediation_steps": {"type": "array", "items": {"type": "string"}},
        "stakeholder_update": {"type": "string"},
    },
    "required": ["summary", "probable_root_cause", "evidence", "affected_assets",
                 "remediation_steps", "stakeholder_update"],
    "additionalProperties": False,
}


def build_context(engine: Engine, incident_id: str) -> dict[str, Any]:
    with engine.lock:
        inc = engine.incidents[incident_id]
        data = engine.incident_dict(inc, full=True)
        reasons = Counter()
        for row in engine.wh.query(
            "SELECT reasons FROM quarantine WHERE tick >= ? ORDER BY tick DESC LIMIT 500",
            (inc.opened_tick - 2,),
        ):
            for reason in json.loads(row["reasons"]):
                reasons[reason] += 1
        recent_errors = [t for t in engine.last_run if t["status"] in ("failed", "no_data", "blocked")]
        similar = [i for i in engine.incidents.values()
                   if i.category == inc.category and i.id != inc.id and i.status == "resolved"]
        tm = engine.settings.tick_minutes
        mttrs = [m for i in similar if (m := i.mttr_minutes(tm)) is not None]
        return {
            "incident": data,
            "upstream": sorted(catalog.ancestors(inc.dataset)),
            "top_quarantine_reasons": reasons.most_common(5),
            "pipeline_task_issues": recent_errors,
            "history": {"similar_resolved": len(similar),
                        "avg_mttr_minutes": round(sum(mttrs) / len(mttrs), 1) if mttrs else None},
        }


def rules_analysis(ctx: dict[str, Any]) -> dict[str, Any]:
    inc = ctx["incident"]
    main = inc["checks"][0] if inc["checks"] else None
    evidence = [f"{c['dataset']}.{c['name']} = {c['value']} ({c['message']})" for c in inc["checks"]]
    evidence += [f"Symptom downstream: {s}" for s in inc["symptoms"][:4]]
    evidence += [f"Quarantine reason ×{n}: {r}" for r, n in ctx["top_quarantine_reasons"][:3]]
    evidence += [f"Task {t['task']} → {t['status']}: {t['note']}" for t in ctx["pipeline_task_issues"][:3] if t["note"]]

    radius = inc["blast_radius"]
    tier1 = [d["id"] for d in radius if d["criticality"] == "tier1"]
    gate = (f"The quality gate held back {', '.join(inc['blocked_models'])}, so consumers keep the last good version."
            if inc["blocked_models"] else "No gold model was blocked; downstream assets may be stale or at risk.")
    history = ctx["history"]
    hist = (f" Seen {history['similar_resolved']} time(s) before; average MTTR {history['avg_mttr_minutes']} min."
            if history["similar_resolved"] else " First occurrence of this failure mode.")
    return {
        "mode": "rules",
        "summary": f"{inc['category_label']} detected on {inc['dataset']} at {inc['opened_at']}. {gate}{hist}",
        "probable_root_cause": (f"{inc['category_label']}: {main['message']}" if main else inc["category_label"]),
        "evidence": evidence,
        "affected_assets": [d["id"] for d in radius],
        "remediation_steps": inc["runbook"]["steps"],
        "stakeholder_update": (
            f"[{inc['severity']}] {inc['category_label']} on {inc['dataset']}. "
            f"{len(radius)} downstream assets affected ({len(tier1)} tier-1: {', '.join(tier1[:3]) or 'none'}). "
            f"Owner: {inc['owner']}. {gate}"
        ),
    }


def claude_analysis(ctx: dict[str, Any], model: str) -> dict[str, Any]:
    import anthropic

    client = anthropic.Anthropic()
    response = client.beta.messages.create(
        model=model,
        max_tokens=16000,
        system=SYSTEM_PROMPT,
        output_config={"effort": "low", "format": {"type": "json_schema", "schema": OUTPUT_SCHEMA}},
        betas=["server-side-fallback-2026-07-01"],
        extra_body={"fallbacks": "default"},
        messages=[{"role": "user", "content": "Incident evidence (JSON):\n" + json.dumps(ctx, default=str)}],
    )
    if response.stop_reason == "refusal":
        raise RuntimeError("model declined the request")
    text = next(b.text for b in response.content if b.type == "text")
    return {"mode": "claude", "model": response.model, **json.loads(text)}


def analyze(engine: Engine, incident_id: str, use_llm: bool | None = None) -> dict[str, Any]:
    ctx = build_context(engine, incident_id)
    if use_llm is None:
        use_llm = bool(os.getenv("ANTHROPIC_API_KEY"))
    if use_llm:
        try:
            return claude_analysis(ctx, engine.settings.copilot_model)
        except Exception as exc:  # the copilot must never break incident response
            log.warning("Claude copilot unavailable, falling back to rules: %s", exc)
            result = rules_analysis(ctx)
            result["fallback_reason"] = str(exc)[:200]
            return result
    return rules_analysis(ctx)
