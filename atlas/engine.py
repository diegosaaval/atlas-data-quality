"""ATLAS engine: runs one pipeline DAG per tick and keeps the reliability state.

One tick == one 5-minute micro-batch in simulated time:

    sources -> bronze -> silver (contracts, dedup, RI, FX) -> checks
            -> quality gate -> gold models -> incidents -> snapshot

The engine is synchronous and deterministic for a given seed; the API layer
drives it from an asyncio loop and streams snapshots over WebSocket.
"""

from __future__ import annotations

import random
import threading
import time
from collections import Counter, deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from . import catalog, checks, runbooks
from .checks import FAIL, WARN, CheckResult
from .config import Settings, get_settings
from .contracts import get_contract
from .faults import FAULTS, FAULTS_BY_ID, ActiveFault
from .sources import SyntheticSources
from .warehouse import GOLD_MODELS, Warehouse

SIM_START = datetime(2026, 10, 1, 6, 0)
COST_PER_1K_ROWS = 0.0025  # USD, rough serverless-compute estimate
COST_PER_TASK = 0.0004
TRACE_STEPS = ("injected", "detected", "gate", "incident", "blast_radius", "proposed", "remediation", "recovered")


@dataclass
class DatasetState:
    data_as_of: datetime | None = None
    last_rows: int = 0
    checks: list[CheckResult] = field(default_factory=list)
    status: str = "healthy"
    score: float = 100.0
    blocked: bool = False


@dataclass
class Incident:
    id: str
    dataset: str
    category: str
    severity: str
    opened_tick: int
    status: str = "open"  # open -> mitigating -> resolved
    checks: list[dict] = field(default_factory=list)
    symptoms: set[str] = field(default_factory=set)
    blocked_models: set[str] = field(default_factory=set)
    timeline: list[dict] = field(default_factory=list)
    fault_ids: list[str] = field(default_factory=list)
    fault_injected_tick: int | None = None
    resolved_tick: int | None = None
    clear_streak: int = 0

    @property
    def title(self) -> str:
        return f"{runbooks.LABELS[self.category]} on {self.dataset}"

    def mttd_minutes(self, tick_minutes: int) -> int | None:
        if self.fault_injected_tick is None:
            return None
        # Detection happens at the end of the run that first carried the fault.
        return (self.opened_tick - self.fault_injected_tick + 1) * tick_minutes

    def mttr_minutes(self, tick_minutes: int) -> int | None:
        if self.resolved_tick is None:
            return None
        return (self.resolved_tick - self.opened_tick) * tick_minutes


class Engine:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.lock = threading.RLock()
        self.running = True
        self.speed = 1.0
        self.chaos = False
        self.reset()

    # ------------------------------------------------------------------ setup
    def reset(self) -> None:
        with self.lock:
            s = self.settings
            self.tick_no = -1
            self.wh = Warehouse(s.db_path)
            self.sources = SyntheticSources(s.seed, SIM_START)
            self.chaos_rng = random.Random(s.seed + 1)
            self.state = {d.id: DatasetState() for d in catalog.DATASETS}
            self.active_faults: dict[str, ActiveFault] = {}
            self.traces: deque[dict] = deque(maxlen=10)
            self.incidents: dict[str, Incident] = {}
            self._incident_seq = 0
            self.events: deque[dict] = deque(maxlen=200)
            self.series: dict[str, deque] = {k: deque(maxlen=180) for k in
                                             ("tick", "score", "ingested", "accepted", "quarantined", "blocked")}
            self.volume_history: deque[int] = deque(maxlen=12)
            self.amount_history: deque[float] = deque(maxlen=24)
            self.slo_window: dict[str, deque[bool]] = {
                d.id: deque(maxlen=s.retention_ticks) for d in catalog.DATASETS if d.layer in ("silver", "gold")}
            self.totals: Counter = Counter()
            self.last_run: list[dict] = []
            self.last_tick_ms = 0.0
            self.copilot_cache: dict[str, dict] = {}

    def sim_time(self, tick: int | None = None) -> datetime:
        return SIM_START + timedelta(minutes=self.settings.tick_minutes * (self.tick_no if tick is None else tick))

    def log(self, level: str, message: str, dataset: str | None = None) -> None:
        self.events.appendleft({"tick": self.tick_no, "time": self.sim_time().strftime("%H:%M"),
                                "level": level, "message": message, "dataset": dataset})

    # ----------------------------------------------------------------- faults
    def inject(self, fault_id: str, auto_heal_ticks: int | None = None, actor: str = "engineer") -> ActiveFault:
        with self.lock:
            spec = FAULTS_BY_ID[fault_id]
            if fault_id in self.active_faults:
                return self.active_faults[fault_id]
            tick = self.tick_no + 1  # takes effect on the next pipeline run
            fault = ActiveFault(spec, tick, tick + auto_heal_ticks if auto_heal_ticks else None)
            self.active_faults[fault_id] = fault
            self.traces.appendleft({"fault": spec.id, "name": spec.name, "detected_on": spec.detected_on,
                                    "steps": {"injected": tick}, "notes": {}})
            self.log("warn", f"Fault injected by {actor}: {spec.name} → {spec.target}", spec.target)
            return fault

    def clear_fault(self, fault_id: str, actor: str = "engineer") -> bool:
        with self.lock:
            fault = self.active_faults.pop(fault_id, None)
            if fault:
                self.log("info", f"Fault cleared by {actor}: {fault.spec.name}", fault.spec.target)
            return fault is not None

    def remediate(self, incident_id: str, actor: str = "engineer") -> Incident:
        with self.lock:
            inc = self.incidents[incident_id]
            cleared = [f for f in inc.fault_ids if self.clear_fault(f, actor)]
            # Organic incidents (no linked fault): clear any fault detected on this dataset.
            for fid, fault in list(self.active_faults.items()):
                if fault.spec.detected_on == inc.dataset:
                    self.clear_fault(fid, actor)
                    cleared.append(fid)
            if inc.status == "open":
                inc.status = "mitigating"
            steps = runbooks.RUNBOOKS[inc.category]["steps"]
            self._timeline(inc, "remediation", f"Remediation applied by {actor}: {steps[min(1, len(steps) - 1)]}")
            for fid in cleared:
                self._trace_step(fid, "remediation", self.tick_no)
            return inc

    def _trace_step(self, fault_id: str, step: str, tick: int, note: str | None = None) -> None:
        for trace in self.traces:
            if trace["fault"] == fault_id:
                if step not in trace["steps"]:
                    trace["steps"][step] = tick
                    if note:
                        trace["notes"][step] = note
                return

    def _maybe_chaos(self) -> None:
        if not self.chaos or self.active_faults or self.chaos_rng.random() > 0.05:
            return
        spec = self.chaos_rng.choice(FAULTS)
        self.inject(spec.id, auto_heal_ticks=self.chaos_rng.randint(6, 12), actor="chaos-monkey")

    def _auto_heal(self, tick: int) -> None:
        for fid, fault in list(self.active_faults.items()):
            if fault.auto_heal_tick is not None and tick >= fault.auto_heal_tick:
                for inc in self.incidents.values():
                    if fid in inc.fault_ids and inc.status == "open":
                        inc.status = "mitigating"
                        self._timeline(inc, "remediation", "Auto-remediated by on-call automation")
                self._trace_step(fid, "remediation", tick)
                self.clear_fault(fid, actor="on-call automation")

    # ------------------------------------------------------------------- tick
    def tick(self) -> dict[str, Any]:
        with self.lock:
            started = time.perf_counter()
            self.tick_no += 1
            t, now = self.tick_no, self.sim_time(self.tick_no)
            self._auto_heal(t)
            self._maybe_chaos()
            faults = {fid for fid, f in self.active_faults.items() if f.injected_tick <= t}

            tasks: list[dict] = []
            results: list[CheckResult] = []
            out = self.sources.emit(t, now, faults)
            for d in catalog.DATASETS:
                if d.layer == "source":
                    self.state[d.id].data_as_of = now

            # ---- ingest -------------------------------------------------
            acc_error = out.errors.get("bronze.accounts")
            if out.accounts is not None:
                n = self.wh.load_bronze("bronze_accounts", f"acc-{t}", t, now.isoformat(), out.accounts.records)
                self._loaded("bronze.accounts", now, n)
                tasks.append(self._task("ingest_accounts", "success", n))
            else:
                tasks.append(self._task("ingest_accounts", "failed", 0, acc_error))
            results.append(checks.pipeline("bronze.accounts", acc_error))

            txn_records: list[dict] = []
            regular: list[dict] = []
            for batch in out.transactions:
                self.wh.load_bronze("bronze_transactions", f"txn-{t}{'-bf' if batch.is_backfill else ''}", t,
                                    now.isoformat(), batch.records, batch.is_backfill)
                txn_records.extend(batch.records)
                if not batch.is_backfill:
                    regular = batch.records
            regular_rows = len(regular)
            if out.transactions:
                self._loaded("bronze.transactions", now, len(txn_records))
                backfill = len(out.transactions) - (1 if regular_rows else 0)
                tasks.append(self._task("ingest_transactions", "success", len(txn_records),
                                        f"{backfill} backfilled partition(s)" if backfill else None))
                if backfill:
                    self.log("info", f"Backfilled {backfill} late partition(s) into bronze.transactions",
                             "bronze.transactions")
            else:
                tasks.append(self._task("ingest_transactions", "no_data", 0, "Expected partition did not arrive"))

            if out.fx is not None:
                self.wh.load_bronze("bronze_fx_rates", f"fx-{t}", t, now.isoformat(), out.fx.records)
                self.wh.insert_fx(out.fx.records, t)
                self._loaded("bronze.fx_rates", now, len(out.fx.records))
                tasks.append(self._task("ingest_fx", "success", len(out.fx.records)))
            else:
                tasks.append(self._task("ingest_fx", "no_data", 0, "Provider returned no quotes"))

            # ---- silver.accounts ---------------------------------------
            acc_contract = get_contract("bronze.accounts")
            if out.accounts is not None:
                observed = set().union(*(r.keys() for r in out.accounts.records)) if out.accounts.records else \
                    acc_contract.column_names
                results.append(checks.schema("bronze.accounts", acc_contract, observed))
                good, bad = [], []
                for r in out.accounts.records:
                    reasons = acc_contract.validate(r)
                    (bad.append((r, reasons)) if reasons else good.append(r))
                self.wh.upsert_accounts(good, t)
                self.wh.quarantine("silver.accounts", f"acc-{t}", t, bad)
                self._derive("silver.accounts", len(good))
                tasks.append(self._task("silver_accounts", "success", len(good)))
            else:
                tasks.append(self._task("silver_accounts", "skipped", 0, "Upstream task failed"))

            # ---- bronze.transactions checks + silver.transactions ------
            txn_contract = get_contract("bronze.transactions")
            silver_accepted = 0
            quarantined = 0
            if txn_records:
                observed = set().union(*(r.keys() for r in txn_records))
                results.append(checks.schema("bronze.transactions", txn_contract, observed))
                nulls = {c: sum(1 for r in txn_records if r.get(c) in (None, ""))
                         for c in txn_contract.required_columns if c in observed}
                results.append(checks.completeness("bronze.transactions", nulls, len(txn_records)))

                ids = [r.get("txn_id") for r in txn_records]
                existing = self.wh.existing_txn_ids([i for i in ids if i])
                seen: set[str] = set()
                unique: list[dict] = []
                dup = 0
                for r in txn_records:
                    tid = r.get("txn_id")
                    if tid in seen or tid in existing:
                        dup += 1
                        continue
                    seen.add(tid)
                    unique.append(r)
                results.append(checks.duplicates("bronze.transactions", dup, len(txn_records)))

                if regular_rows:
                    vol = checks.volume("bronze.transactions", regular_rows, self.volume_history)
                    results.append(vol)
                    if vol.status != FAIL:  # never learn the baseline from anomalies
                        self.volume_history.append(regular_rows)
                    # Reconcile only rows that are new to the platform (replays are not the replica's problem).
                    distinct = {r.get("txn_id"): r for r in regular if r.get("txn_id") not in existing}.values()
                    results.append(checks.reconciliation(
                        "bronze.transactions", len(distinct),
                        sum(float(r.get("amount", r.get("amount_value")) or 0) for r in distinct), out.replica))

                fx = self.wh.latest_fx() or 4000.0
                known = self.wh.known_accounts({r["account_id"] for r in unique if r.get("account_id")})
                good, bad, orphans = [], [], 0
                for r in unique:
                    reasons = txn_contract.validate(r)
                    if r.get("account_id") and r["account_id"] not in known:
                        reasons.append("account_id: unknown account")
                        orphans += 1
                    if reasons:
                        bad.append((r, reasons))
                        continue
                    amount_cop = r["amount"] * fx if r["currency"] == "USD" else r["amount"]
                    good.append({**r, "amount_cop": round(amount_cop, 2)})
                silver_accepted = self.wh.insert_transactions(good, t)
                quarantined = len(bad)
                self.wh.quarantine("silver.transactions", f"txn-{t}", t, bad)
                results.append(checks.validity("silver.transactions", quarantined, len(unique)))
                results.append(checks.integrity("silver.transactions", orphans, len(unique)))
                if good:
                    mean_amount = sum(g["amount_cop"] for g in good) / len(good)
                    dist = checks.distribution("silver.transactions", mean_amount, self.amount_history)
                    results.append(dist)
                    if dist.status == "pass":
                        self.amount_history.append(mean_amount)
                self._derive("silver.transactions", silver_accepted)
                tasks.append(self._task("silver_transactions", "success", silver_accepted,
                                        f"{quarantined} quarantined" if quarantined else None))
            else:
                tasks.append(self._task("silver_transactions", "skipped", 0, "No new bronze data"))

            # ---- freshness for bronze & silver ------------------------
            for d in catalog.DATASETS:
                if d.layer in ("bronze", "silver"):
                    results.append(checks.freshness(d.id, self._age(d.id, now), d.sla_minutes,
                                                    self.settings.tick_minutes))
            tasks.append(self._task("quality_checks", "success", len(results)))

            # ---- quality gate + gold ----------------------------------
            blockers = [r for r in results if r.blocking]
            blocked_models: dict[str, list[CheckResult]] = {}
            since = t - 60 // self.settings.tick_minutes + 1
            for model in GOLD_MODELS:
                model_blockers = [b for b in blockers if b.dataset in catalog.ancestors(model)]
                state = self.state[model]
                if model_blockers:
                    blocked_models[model] = model_blockers
                    state.blocked = True
                    tasks.append(self._task(f"build_{model.split('.')[1]}", "blocked", 0,
                                            f"Quality gate: {model_blockers[0].dataset}.{model_blockers[0].name}"))
                else:
                    state.blocked = False
                    rows = self.wh.build_gold(model, since)
                    self._derive(model, rows)
                    tasks.append(self._task(f"build_{model.split('.')[1]}", "success", rows))
            gate_status = "blocked" if blocked_models else "success"
            tasks.insert(len(tasks) - len(GOLD_MODELS),
                         self._task("quality_gate", gate_status, len(blockers),
                                    f"{len(blocked_models)} model(s) held back" if blocked_models else None))
            for model in GOLD_MODELS:
                d = catalog.get(model)
                results.append(checks.freshness(model, self._age(model, now), d.sla_minutes,
                                                self.settings.tick_minutes))

            # ---- state, scores, incidents -----------------------------
            self._update_state(results)
            self.wh.record_checks(t, results)
            self._update_incidents(results, blocked_models)
            self._update_series(regular_rows or len(txn_records), silver_accepted, quarantined,
                                silver_accepted if blocked_models else 0)
            if t % 12 == 0:
                self.wh.prune(t - self.settings.retention_ticks)
            self.last_run = tasks
            self.last_tick_ms = (time.perf_counter() - started) * 1000
            return self.snapshot()

    # --------------------------------------------------------------- helpers
    @staticmethod
    def _task(name: str, status: str, rows: int, note: str | None = None) -> dict:
        cost = COST_PER_TASK + rows / 1000 * COST_PER_1K_ROWS if status == "success" else 0.0
        return {"task": name, "status": status, "rows": rows, "note": note, "cost_usd": round(cost, 6)}

    def _loaded(self, dataset_id: str, when: datetime, rows: int) -> None:
        state = self.state[dataset_id]
        state.data_as_of = when
        state.last_rows = rows

    def _derive(self, dataset_id: str, rows: int) -> None:
        """A derived dataset is only as fresh as its stalest input."""
        ups = [self.state[u].data_as_of for u in catalog.get(dataset_id).upstream]
        self.state[dataset_id].data_as_of = None if any(u is None for u in ups) else min(ups)
        self.state[dataset_id].last_rows = rows

    def _age(self, dataset_id: str, now: datetime) -> float | None:
        as_of = self.state[dataset_id].data_as_of
        return None if as_of is None else (now - as_of).total_seconds() / 60

    def _update_state(self, results: list[CheckResult]) -> None:
        by_ds: dict[str, list[CheckResult]] = {}
        for r in results:
            by_ds.setdefault(r.dataset, []).append(r)
        for d in catalog.DATASETS:
            st = self.state[d.id]
            if d.layer == "source":
                st.status = "fault" if any(f.spec.target == d.id for f in self.active_faults.values()) else "healthy"
                continue
            if d.layer == "consumer":
                bad = [u for u in d.upstream if self.state[u].status in ("critical", "blocked")]
                st.status = "degraded" if bad else "healthy"
                st.score = min(self.state[u].score for u in d.upstream)
                continue
            st.checks = by_ds.get(d.id, st.checks)
            st.score = checks.dataset_score(st.checks)
            if any(c.status == FAIL for c in st.checks):
                st.status = "critical"
            elif st.blocked:
                st.status = "blocked"
            elif any(c.status == WARN for c in st.checks):
                st.status = "warning"
            else:
                st.status = "healthy"
            if st.blocked:
                st.score = max(0.0, st.score - 20)
            if d.id in self.slo_window:
                fresh = next((c for c in st.checks if c.name == "freshness"), None)
                self.slo_window[d.id].append(fresh is None or fresh.status != FAIL)

    def _timeline(self, inc: Incident, kind: str, message: str) -> None:
        inc.timeline.append({"tick": self.tick_no, "time": self.sim_time().strftime("%H:%M"),
                             "kind": kind, "message": message})

    @staticmethod
    def _severity(dataset_id: str, stops_data: bool) -> str:
        """SEV1: data stopped for a regulatory product. SEV2: tier-1 at risk. SEV3: everything else."""
        impacted = {dataset_id, *catalog.descendants(dataset_id)}
        if stops_data and "reg.regulatory_report" in impacted:
            return "SEV1"
        if any(catalog.get(i).criticality == "tier1" for i in impacted):
            return "SEV2"
        return "SEV3"

    def _update_incidents(self, results: list[CheckResult], blocked_models: dict[str, list[CheckResult]]) -> None:
        t = self.tick_no
        failing = [r for r in results if r.status == FAIL]
        failing_ds = {r.dataset for r in failing}
        roots: dict[str, list[CheckResult]] = {}
        symptoms: list[CheckResult] = []
        for r in failing:
            if catalog.ancestors(r.dataset) & failing_ds:
                symptoms.append(r)
            else:
                roots.setdefault(r.dataset, []).append(r)

        open_by_ds = {i.dataset: i for i in self.incidents.values() if i.status != "resolved"}
        for ds, ds_checks in roots.items():
            main = runbooks.primary(ds_checks)
            inc = open_by_ds.get(ds)
            if inc is None:
                inc = self._open_incident(ds, main)
                open_by_ds[ds] = inc
            inc.checks = [c.to_dict() for c in sorted(ds_checks, key=lambda c: runbooks.PRIORITY.index(c.dimension))]
            inc.clear_streak = 0

        for s in symptoms:
            for inc in open_by_ds.values():
                if inc.dataset in catalog.ancestors(s.dataset):
                    inc.symptoms.add(f"{s.dataset} · {s.name}")

        for inc in open_by_ds.values():
            newly = sorted(
                model for model, model_blockers in blocked_models.items()
                if model not in inc.blocked_models and any(
                    b.dataset == inc.dataset or inc.dataset in catalog.ancestors(b.dataset) for b in model_blockers)
            )
            if not newly:
                continue
            inc.blocked_models.update(newly)
            self._timeline(inc, "gate", f"Quality gate held back {', '.join(newly)}; last good version kept")
            escalated = self._severity(inc.dataset, stops_data=True)
            if escalated < inc.severity:
                self._timeline(inc, "escalated", f"Severity escalated {inc.severity} → {escalated}")
                inc.severity = escalated
            for fid in inc.fault_ids:
                self._trace_step(fid, "gate", t)

        for inc in list(open_by_ds.values()):
            if inc.dataset in roots:
                continue
            inc.clear_streak += 1
            if inc.clear_streak >= 2:
                inc.status = "resolved"
                inc.resolved_tick = t
                self._timeline(inc, "recovered", "All checks green for 2 consecutive runs — incident resolved")
                self.log("success", f"{inc.id} resolved: {inc.title}", inc.dataset)
                self.totals["incidents_resolved"] += 1
                for fid in inc.fault_ids:
                    self._trace_step(fid, "recovered", t)

    def _open_incident(self, ds: str, main: CheckResult) -> Incident:
        t = self.tick_no
        self._incident_seq += 1
        category = runbooks.classify(main)
        inc = Incident(id=f"INC-{self._incident_seq:04d}", dataset=ds, category=category,
                       severity=self._severity(ds, main.blocking or main.dimension == "pipeline"),
                       opened_tick=t)
        linked = [f for f in self.active_faults.values()
                  if f.spec.detected_on == ds or f.spec.target in catalog.ancestors(ds)]
        if linked:
            inc.fault_ids = [f.spec.id for f in linked]
            inc.fault_injected_tick = min(f.injected_tick for f in linked)
        self.incidents[inc.id] = inc
        self._timeline(inc, "detected", f"{main.name} failed: {main.message}")
        radius = catalog.blast_radius(ds)
        self._timeline(inc, "blast_radius", f"Blast radius: {len(radius)} downstream assets "
                                            f"({sum(1 for d in radius if d.criticality == 'tier1')} tier-1)")
        self._timeline(inc, "remediation", f"Runbook attached: {runbooks.RUNBOOKS[category]['title']}")
        self.log("error", f"{inc.id} opened [{inc.severity}] {inc.title}", ds)
        self.totals["incidents_opened"] += 1
        for f in linked:
            for step in ("detected", "incident", "blast_radius"):
                self._trace_step(f.spec.id, step, t)
            self._trace_step(f.spec.id, "proposed", t)
            if main.dimension not in checks.BLOCKING:
                self._trace_step(f.spec.id, "gate", t,
                                 "Non-blocking check: data keeps flowing, downstream flagged stale/at-risk")
        return inc

    def _update_series(self, ingested: int, accepted: int, quarantined: int, blocked: int) -> None:
        self.totals["rows_ingested"] += ingested
        self.totals["rows_accepted"] += accepted
        self.totals["rows_quarantined"] += quarantined
        self.totals["rows_blocked"] += blocked
        for key, value in (("tick", self.tick_no), ("score", round(self.platform_score(), 1)),
                           ("ingested", ingested), ("accepted", accepted),
                           ("quarantined", quarantined), ("blocked", blocked)):
            self.series[key].append(value)

    # -------------------------------------------------------------- read side
    def platform_score(self) -> float:
        scored = [d for d in catalog.DATASETS if d.layer in ("bronze", "silver", "gold")]
        total = sum(d.weight for d in scored)
        return sum(self.state[d.id].score * d.weight for d in scored) / total

    def freshness_slo(self) -> float:
        tier1 = [d.id for d in catalog.DATASETS if d.id in self.slo_window and d.criticality == "tier1"]
        samples = [ok for d in tier1 for ok in self.slo_window[d]]
        return 100.0 * sum(samples) / len(samples) if samples else 100.0

    def incident_dict(self, inc: Incident, full: bool = False) -> dict[str, Any]:
        tm = self.settings.tick_minutes
        data = {
            "id": inc.id, "title": inc.title, "dataset": inc.dataset, "category": inc.category,
            "category_label": runbooks.LABELS[inc.category], "severity": inc.severity, "status": inc.status,
            "opened_tick": inc.opened_tick, "opened_at": self.sim_time(inc.opened_tick).strftime("%H:%M"),
            "resolved_tick": inc.resolved_tick, "owner": catalog.get(inc.dataset).owner,
            "mttd_minutes": inc.mttd_minutes(tm), "mttr_minutes": inc.mttr_minutes(tm),
            "fault_ids": inc.fault_ids, "blocked_models": sorted(inc.blocked_models),
        }
        if full:
            data |= {
                "checks": inc.checks,
                "symptoms": sorted(inc.symptoms),
                "timeline": inc.timeline,
                "blast_radius": [{"id": d.id, "name": d.name, "layer": d.layer, "criticality": d.criticality,
                                  "owner": d.owner} for d in catalog.blast_radius(inc.dataset)],
                "runbook": runbooks.RUNBOOKS[inc.category],
            }
        return data

    def dataset_dict(self, dataset_id: str) -> dict[str, Any]:
        d = catalog.get(dataset_id)
        st = self.state[dataset_id]
        age = self._age(dataset_id, self.sim_time()) if self.tick_no >= 0 else None
        slo = self.slo_window.get(dataset_id)
        return {
            "id": d.id, "name": d.name, "layer": d.layer, "domain": d.domain, "owner": d.owner,
            "criticality": d.criticality, "sla_minutes": d.sla_minutes, "description": d.description,
            "upstream": list(d.upstream), "status": st.status, "score": round(st.score, 1),
            "freshness_minutes": None if age is None else round(age, 1), "last_rows": st.last_rows,
            "blocked": st.blocked, "checks": [c.to_dict() for c in st.checks],
            "slo": None if not slo else round(100 * sum(slo) / len(slo), 2),
        }

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            tm = self.settings.tick_minutes
            incidents = sorted(self.incidents.values(), key=lambda i: (i.status == "resolved", -i.opened_tick))
            resolved = [i for i in self.incidents.values() if i.status == "resolved"]
            mttd = [m for i in self.incidents.values() if (m := i.mttd_minutes(tm)) is not None]
            mttr = [m for i in resolved if (m := i.mttr_minutes(tm)) is not None]
            datasets = [self.dataset_dict(d.id) for d in catalog.DATASETS]
            run_cost = sum(task["cost_usd"] for task in self.last_run)
            return {
                "tick": self.tick_no,
                "sim_time": self.sim_time().isoformat() if self.tick_no >= 0 else None,
                "running": self.running, "speed": self.speed, "chaos": self.chaos,
                "platform": {
                    "score": round(self.platform_score(), 1),
                    "freshness_slo": round(self.freshness_slo(), 2),
                    "open_incidents": sum(1 for i in self.incidents.values() if i.status != "resolved"),
                    "datasets_healthy": sum(1 for d in datasets if d["status"] == "healthy"),
                    "datasets_total": len(datasets),
                    "mttd_minutes": round(sum(mttd) / len(mttd), 1) if mttd else None,
                    "mttr_minutes": round(sum(mttr) / len(mttr), 1) if mttr else None,
                    "rows_ingested": self.totals["rows_ingested"],
                    "rows_quarantined": self.totals["rows_quarantined"],
                    "rows_blocked": self.totals["rows_blocked"],
                    "run_cost_usd": round(run_cost, 4),
                    "daily_cost_usd": round(run_cost * 24 * 60 / tm, 2),
                    "tick_ms": round(self.last_tick_ms, 1),
                },
                "datasets": datasets,
                "edges": catalog.edges(),
                "incidents": [self.incident_dict(i) for i in incidents[:25]],
                "faults": [{"id": f.id, "name": f.name, "description": f.description, "target": f.target,
                            "detected_on": f.detected_on, "root_cause": f.root_cause,
                            "active": f.id in self.active_faults} for f in FAULTS],
                "traces": [{**tr, "steps": {k: (self.sim_time(v).strftime("%H:%M"), v)
                                            for k, v in tr["steps"].items()}} for tr in self.traces],
                "dag": self.last_run,
                "series": {k: list(v) for k, v in self.series.items()},
                "events": list(self.events)[:40],
            }
