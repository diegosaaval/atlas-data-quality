"""ATLAS engine: plays the morning, validates each table as it lands, manages incidents.

Simulated clock: every tick is 15 minutes between 05:30 and 10:30. Tables arrive around
their agreed time; ATLAS validates them on arrival, flags the ones that do not show up,
and opens one incident per table with everything the owning team needs to act.
"""

from __future__ import annotations

import random
import statistics
import threading
from collections import deque
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any
from urllib.parse import quote

from . import monitors
from .config import Settings, get_settings
from .monitors import GRACE_MINUTES, hhmm
from .rules import FAIL, OK, SEVERITY_LABEL, SEVERITY_WEIGHT, WARN, CheckResult, Rule, RuleStore, baseline, evaluate
from .store import Store
from .tables import BANK_TABLES, BY_NAME, DAYS_ES, SCENARIOS, SCENARIOS_BY_ID, TABLES, Load, SyntheticBank, use_tables

DAY_START, DAY_END, STEP = 5 * 60 + 30, 10 * 60 + 30, 15
HISTORY_DAYS = 70
# Past incidents so the history has something to show: days before today -> (table, scenario)
SEEDED_HISTORY = {40: "ingesta_borrada", 33: "carga_duplicada", 26: "pico_desembolsos",
                  19: "tasa_mora_negativa", 12: "nulos_documento", 5: "llega_tarde"}
STATUS_LABEL = {"esperando": "Esperando", "retrasada": "Retrasada", "no_disponible": "No disponible",
                "ok": "OK", "advertencia": "Advertencia", "falla": "Con fallas"}


@dataclass
class TableState:
    load: Load
    status: str = "esperando"
    arrived: int | None = None
    results: list[CheckResult] = field(default_factory=list)
    score: float | None = None
    scenario: str | None = None


@dataclass
class Incident:
    id: str
    table: str
    severity: str
    opened: tuple[str, int]  # (iso date, minute)
    title: str
    status: str = "abierto"  # abierto -> escalado -> resuelto
    checks: list[dict] = field(default_factory=list)
    timeline: list[dict] = field(default_factory=list)
    resolved: tuple[str, int] | None = None
    resolution: str | None = None
    scenario: str | None = None
    loads_failed: int = 1
    run_id: str | None = None  # corrida de la fuente (p. ej. MIDAS) que trajo la carga con falla
    _last_day: str = ""

    def minutes_open(self, now: tuple[str, int]) -> int:
        end = self.resolved or now
        days = (date.fromisoformat(end[0]) - date.fromisoformat(self.opened[0])).days
        return days * 1440 + end[1] - self.opened[1]


class Engine:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.lock = threading.RLock()
        self.running = True
        self.speed = 1.0
        self.random_anomalies = self.settings.random_anomalies
        self.reset()

    # ------------------------------------------------------------------ setup
    def reset(self) -> None:
        with self.lock:
            s = self.settings
            if getattr(self, "store", None) is not None:
                self.store.close()
            # Fuente: banco simulado (demo) o un conector con tablas reales (conectores/<nombre>.yaml).
            self.source = None
            self._reprocessed_at: datetime | None = None
            self._run_id: str | None = None
            if s.source:
                from .connectors import load_source

                self.source = load_source(s.source, s.source_config, s.source_path)
                use_tables(self.source.tables)
            else:
                use_tables(BANK_TABLES)
            self.store = Store(s.db_path)
            rules_path = s.rules_path
            if self.source and rules_path == "data/reglas.json":
                rules_path = f"data/reglas_{self.source.name}.json"
            self.rules = RuleStore(rules_path, defaults=self.source.rules if self.source else None)
            self.bank = SyntheticBank(s.seed)
            self.rng = random.Random(s.seed + 1)
            self.today: date = s.start_date - timedelta(days=HISTORY_DAYS)
            self.minute = DAY_START
            self.tables: dict[str, TableState] = {}
            self.incidents: dict[str, Incident] = {}
            self._seq = 0
            self.events: deque[dict] = deque(maxlen=150)
            self.queued: dict[str, str] = {}
            self.copilot_cache: dict[str, dict] = {}
            if self.source:
                self._reset_source()
                return
            self._bootstrapping = True
            for back in range(HISTORY_DAYS, 0, -1):
                scenario = SEEDED_HISTORY.get(back)
                if scenario:
                    self.queued[SCENARIOS_BY_ID[scenario].table] = scenario
                self._start_day(self.today)
                while self.minute < DAY_END:
                    self._advance()
                self._close_day()
                self.today += timedelta(days=1)
            self._bootstrapping = False
            self.events.clear()
            self._start_day(self.today)

    def close(self) -> None:
        self.store.close()

    # -------------------------------------------------------- real data source
    def _reset_source(self) -> None:
        now = datetime.now()
        self.today, self.minute = now.date(), now.hour * 60 + now.minute
        self.tables = {t.name: TableState(Load(t.name, [], [], None)) for t in TABLES}
        self._fingerprint = ""
        self._processed: set[str] = set()
        self._overdue_day: str | None = None
        self.source_info: dict[str, Any] = {"status": "esperando", "published_at": None, "run_id": None,
                                            "dates": 0, "last_date": None}
        self._bootstrapping = True
        self._sync()
        self._bootstrapping = False
        if self.source_info["dates"]:
            self.log("success", f"Conectado a {self.source.title}: {self.source_info['dates']} fechas validadas "
                                f"(publicación {self.source_info['run_id'] or self.source_info['published_at']}).")
        else:
            self.log("warn", f"Conectado a {self.source.title}, esperando su primera publicación.")

    def _sync(self) -> bool:
        """Valida lo que la fuente haya publicado desde la última vez. True si hubo algo nuevo."""
        src = self.source
        fp = src.fingerprint()
        if not fp or fp == self._fingerprint:
            return False
        manifest = src.read_manifest()
        if manifest:
            published = datetime.fromisoformat(str(manifest["published_at"])).astimezone()
            dates = sorted({date.fromisoformat(d) for d in manifest.get("dates", [])}) or [published.date()]
            if self._bootstrapping:  # al conectarse, valida toda la historia publicada, no solo la última corrida
                dates = sorted(set(dates) | {d for d in src.available_dates() if d <= dates[-1]})
        else:
            published = datetime.fromtimestamp(max(f.stat().st_mtime for f in src.files.values() if f.exists()))
            dates = [published.date()]
        pending = [d for d in dates if d.isoformat() not in self._processed] or dates
        minute = published.hour * 60 + published.minute
        self._run_id = (manifest or {}).get("run_id")
        for i, day in enumerate(pending):
            # Una fecha publicada días después (backfill o re-proceso) no se juzga por la hora de llegada:
            # la puntualidad se mide en la publicación diaria normal (el mismo día o el siguiente).
            self._reprocessed_at = published if published.date() > day + timedelta(days=1) else None
            last = i == len(pending) - 1
            self.today, self.minute = day, minute
            iso = day.isoformat()
            self.tables = {}
            for spec in TABLES:
                snapshot = src.load_types.get(spec.name) != "incremental"
                if snapshot and not last:
                    continue  # una foto completa solo se valida en la fecha más reciente
                rows, cols = src.read(spec, None if snapshot else day)
                self.tables[spec.name] = TableState(Load(spec.name, rows, cols or spec.column_names, minute))
            for spec in TABLES:
                if spec.name not in self.tables:
                    continue
                self._suggest_rules(spec.name)
                self.store.forget_day(spec.name, iso)
                self._deliver(spec.name, minute)
            self.store.forget_day("_global", iso)
            self._close_day()
            self._processed.add(iso)
        self._reprocessed_at = None
        self._fingerprint = fp
        self._overdue_day = None
        self.source_info = {"status": "ok", "published_at": published.isoformat(timespec="minutes"),
                            "run_id": (manifest or {}).get("run_id"), "dates": len(self._processed),
                            "last_date": max(self._processed), "datasets": (manifest or {}).get("datasets", {}),
                            "run_url": self._run_url(self._run_id)}
        if not self._bootstrapping:
            self.log("info", f"{src.title} publicó {len(pending)} fecha(s) nuevas; validadas.")
        return True

    def _run_url(self, run_id: str | None) -> str | None:
        """Enlace a la corrida en la herramienta de la fuente (p. ej. la pantalla de etapas de MIDAS)."""
        if not (self.source and self.source.run_url and run_id):
            return None
        return self.source.run_url.replace("{run_id}", quote(str(run_id), safe=""))

    def _suggest_rules(self, name: str) -> None:
        """Tablas sin reglas: ATLAS lee su perfil y propone reglas iniciales (una sola vez)."""
        from .connectors import suggest_rules

        if name not in self.source.auto_rules or self.rules.for_table(name, enabled_only=False):
            return
        for raw in suggest_rules(BY_NAME[name], self.tables[name].load.rows):
            try:
                self.rules.add(Rule(id=self.rules.next_id(), author="sugerida", **raw))
            except ValueError:
                continue

    def _check_overdue(self) -> None:
        """Si la fuente no publicó hoy antes de la hora acordada (+ gracia), es un incidente."""
        if not self.source_info.get("published_at"):
            return
        now = datetime.now()
        published = datetime.fromisoformat(self.source_info["published_at"])
        deadline = self.source.expected_at + GRACE_MINUTES
        today = now.date().isoformat()
        if published.date() >= now.date() or now.hour * 60 + now.minute < deadline or self._overdue_day == today:
            return
        self._overdue_day = today
        for spec in TABLES:
            check = CheckResult(spec.name, "disponibilidad", "Disponibilidad de la tabla del día", "monitor", "critica",
                                FAIL, f"{self.source.title} no ha publicado hoy. Última publicación: "
                                      f"{published:%Y-%m-%d %H:%M}.", threshold=f"antes de {hhmm(deadline)}",
                                rule_type="disponibilidad")
            self.tables[spec.name].results = [check]
            self.tables[spec.name].status = "no_disponible"
            self._update_incident(spec.name, [check])
        self.log("error", f"{self.source.title} no ha publicado hoy (se esperaba antes de {hhmm(deadline)}).")

    @property
    def now(self) -> tuple[str, int]:
        return self.today.isoformat(), self.minute

    def log(self, level: str, message: str, table: str | None = None) -> None:
        if not self._bootstrapping:
            self.events.appendleft({"date": self.today.isoformat(), "time": hhmm(self.minute), "level": level,
                                    "message": message, "table": table})

    # -------------------------------------------------------------- the clock
    def tick(self) -> dict[str, Any]:
        with self.lock:
            if self.source:
                self._sync()
                self._check_overdue()
                return self.snapshot()
            if self.minute >= DAY_END:
                self._close_day()
                self.today += timedelta(days=1)
                self._start_day(self.today)
            else:
                self._advance()
            return self.snapshot()

    def run_day(self) -> None:
        """Advance to the end of the current day (tests / 'skip to end of day')."""
        if self.source:
            raise ValueError("Con una fuente real el día no se simula: ATLAS espera sus publicaciones.")
        with self.lock:
            while self.minute < DAY_END:
                self._advance()

    def _start_day(self, day: date) -> None:
        if self.random_anomalies and not self._bootstrapping and self.rng.random() < 0.3:
            pick = self.rng.choice(SCENARIOS)
            self.queued.setdefault(pick.table, pick.id)
        scenarios, self.queued = self.queued, {}
        loads = self.bank.generate_day(day, scenarios)
        self.minute = DAY_START
        self.tables = {name: TableState(load, scenario=scenarios.get(name)) for name, load in loads.items()}
        self.log("info", f"Nuevo día: {DAYS_ES[day.weekday()]} {day.isoformat()}. Esperando {len(TABLES)} cargas.")

    def _advance(self) -> None:
        self.minute += STEP
        for spec in TABLES:
            st = self.tables[spec.name]
            if st.arrived is not None:
                continue
            arrives = st.load.arrives_at
            if arrives is not None and arrives <= self.minute:
                self._deliver(spec.name, arrives)
            elif self.minute >= spec.expected_at + GRACE_MINUTES and st.status != "no_disponible":
                st.status = "no_disponible"
                st.results = [monitors.availability(spec, None, self.minute)]
                st.score = 0.0
                self.log("error", f"{spec.name} no ha llegado (esperada {spec.expected_hhmm}).", spec.name)
                self._update_incident(spec.name, st.results)
            elif self.minute >= spec.expected_at and st.status == "esperando":
                st.status = "retrasada"
                self.log("warn", f"{spec.name} aún no llega (esperada {spec.expected_hhmm}).", spec.name)

    def _deliver(self, name: str, arrived: int) -> None:
        spec = BY_NAME[name]
        st = self.tables[name]
        iso = self.today.isoformat()
        st.arrived = arrived
        rows = st.load.rows
        self.store.insert_load(spec, iso, hhmm(arrived), rows, st.load.columns)
        availability = monitors.availability(spec, arrived, self.minute)
        reprocessed = self._reprocessed_at
        if reprocessed is not None:
            availability.status, availability.value = OK, 0
            availability.message = (f"Fecha publicada en un re-proceso ({reprocessed:%Y-%m-%d %H:%M}); "
                                    "la puntualidad se mide en la publicación diaria.")
        results = [availability,
                   monitors.volume(spec, len(rows), self.store, self.today),
                   monitors.structure(spec, st.load.columns)]
        results += [evaluate(rule, self.store, self.today, len(rows)) for rule in self.rules.for_table(name)]
        st.results = results
        st.score = self._score(results)
        failing = [r for r in results if r.status == FAIL]
        st.status = "falla" if failing else "advertencia" if any(r.status in (WARN, "error") for r in results) else "ok"
        self.store.put_metric(iso, name, "score", st.score)
        self.store.record_checks(iso, results)
        n_rules = sum(1 for r in results if r.kind == "regla")
        if failing:
            self.log("error", f"{name} llegó ({len(rows):,} filas): {len(failing)} control(es) fallaron."
                     .replace(",", "."), name)
        else:
            self.log("success", f"{name} llegó a las {hhmm(arrived)} ({len(rows):,} filas) · {n_rules} reglas OK."
                     .replace(",", "."), name)
        self._update_incident(name, results)

    def _close_day(self) -> None:
        iso = self.today.isoformat()
        for name, st in self.tables.items():
            if st.arrived is None:  # never arrived: record the failure for the history
                self.store.put_metric(iso, name, "score", 0.0)
                self.store.record_checks(iso, st.results or [monitors.availability(BY_NAME[name], None, DAY_END)])
        scores = [st.score for st in self.tables.values() if st.score is not None]
        if scores:
            self.store.put_metric(iso, "_global", "score", statistics.fmean(scores))
        self.store.prune_rows((self.today - timedelta(days=7)).isoformat())
        # Long-running public demo: forget incidents resolved more than 120 days ago.
        horizon = (self.today - timedelta(days=120)).isoformat()
        if self.today.day == 1:
            self.store.prune_history(horizon)
        for key in [k for k, i in self.incidents.items() if i.resolved and i.resolved[0] < horizon]:
            del self.incidents[key]
            self.copilot_cache.pop(key, None)

    @staticmethod
    def _score(results: list[CheckResult]) -> float:
        total = sum(SEVERITY_WEIGHT[r.severity] for r in results)
        got = sum(SEVERITY_WEIGHT[r.severity] * (1 if r.status == OK else 0.5 if r.status == WARN else 0)
                  for r in results)
        return round(100 * got / total, 1) if total else 100.0

    # -------------------------------------------------------------- incidents
    def _timeline(self, inc: Incident, kind: str, text: str, at: tuple[str, int] | None = None) -> None:
        day, minute = at or (self.today.isoformat(), self.minute)
        inc.timeline.append({"date": day, "time": hhmm(minute), "kind": kind, "text": text})

    def _open_incident(self, name: str) -> Incident | None:
        return next((i for i in self.incidents.values() if i.table == name and i.resolved is None), None)

    def _update_incident(self, name: str, results: list[CheckResult]) -> None:
        failing = [r for r in results if r.status == FAIL]
        inc = self._open_incident(name)
        iso = self.today.isoformat()
        spec = BY_NAME[name]
        if failing:
            failing.sort(key=lambda r: -SEVERITY_WEIGHT[r.severity])
            main = failing[0]
            if inc is None:
                self._seq += 1
                inc = Incident(f"INC-{self._seq:04d}", name, main.severity, self.now,
                               f"{main.name}", scenario=self.tables[name].scenario, run_id=self._run_id,
                               _last_day=iso)
                self.incidents[inc.id] = inc
                self._timeline(inc, "detectado", f"Detectado: {main.message}")
                if main.severity == "critica":
                    self._timeline(inc, "notificado",
                                   f"Notificación automática a {spec.owner} ({spec.owner_email}) por severidad crítica.")
                self.log("error", f"{inc.id} abierto [{SEVERITY_LABEL[inc.severity]}] {name}: {main.name}", name)
            elif inc._last_day != iso:
                inc.loads_failed += 1
                inc._last_day = iso
                inc.run_id = self._run_id or inc.run_id
                self._timeline(inc, "persiste", f"La carga del {iso} sigue fallando: {main.message}")
            if SEVERITY_WEIGHT[main.severity] > SEVERITY_WEIGHT[inc.severity]:
                self._timeline(inc, "escalado", f"Severidad sube a {SEVERITY_LABEL[main.severity]}.")
                inc.severity = main.severity
            inc.checks = [r.to_dict() for r in failing]
        elif inc is not None:
            arrived = self.tables[name].arrived
            reason = (f"La tabla llegó a las {hhmm(arrived)} y todos los controles pasan."
                      if any(c["check_id"] == "disponibilidad" for c in inc.checks)
                      else f"La carga del {iso} cumple todos los controles.")
            self._resolve(inc, "automática", reason)

    def _resolve(self, inc: Incident, mode: str, text: str) -> None:
        inc.status = "resuelto"
        # Con un conector, "ahora" es la fecha de los datos: si la fuente re-publica fechas anteriores
        # (un backfill), el cierre no puede quedar antes de la apertura.
        inc.resolved = max(self.now, inc.opened)
        inc.resolution = mode
        self._timeline(inc, "resuelto", f"Resuelto ({mode}): {text}", at=inc.resolved)
        self.log("success", f"{inc.id} resuelto: {inc.table}", inc.table)

    def escalate(self, incident_id: str, actor: str = "analista") -> Incident:
        with self.lock:
            inc = self.incidents[incident_id]
            spec = BY_NAME[inc.table]
            if inc.status == "abierto":
                inc.status = "escalado"
            self._timeline(inc, "escalado", f"Escalado por {actor} a {spec.owner} ({spec.owner_email}).")
            self.log("warn", f"{inc.id} escalado a {spec.owner}", inc.table)
            return inc

    def resolve(self, incident_id: str, note: str, actor: str = "analista") -> Incident:
        with self.lock:
            inc = self.incidents[incident_id]
            if inc.resolved is None:
                self._resolve(inc, "manual", f"{note or 'Sin comentario'} — {actor}")
            return inc

    # -------------------------------------------------------------- scenarios
    def inject(self, scenario_id: str) -> dict[str, str]:
        """Apply a bad load: today if the table has not arrived yet, otherwise tomorrow."""
        if self.source:
            raise ValueError("Las anomalías simuladas solo existen en la demo; aquí los datos son reales.")
        with self.lock:
            sc = SCENARIOS_BY_ID[scenario_id]
            st = self.tables[sc.table]
            if st.arrived is None and st.status != "no_disponible":
                self.bank._apply(sc.id, st.load, self.today)
                st.scenario = sc.id
                when = "hoy"
            else:
                self.queued[sc.table] = sc.id
                when = "mañana"
            self.log("warn", f"Anomalía simulada para {when}: {sc.title}.", sc.table)
            return {"scenario": sc.id, "table": sc.table, "when": when}

    # -------------------------------------------------------------- read side
    def table_summary(self, name: str) -> dict[str, Any]:
        spec = BY_NAME[name]
        st = self.tables[name]
        normal = baseline(self.store.metric_history(name, "filas:base", self.today.isoformat(), 60), self.today,
                          spec.load_type == "incremental")
        spark = self.store.query(
            "SELECT fecha, valor FROM metrics WHERE tabla = ? AND metrica = 'score' ORDER BY fecha DESC LIMIT 30",
            (name,))
        rules = [r for r in st.results if r.kind == "regla"]
        return {
            "name": name, "title": spec.title, "owner": spec.owner, "expected_at": spec.expected_hhmm,
            "status": st.status, "status_label": STATUS_LABEL[st.status],
            "arrived_at": hhmm(st.arrived) if st.arrived is not None else None,
            "rows": len(st.load.rows) if st.arrived is not None else None,
            "expected_rows": round(normal[0]) if normal else None,
            "score": st.score,
            "checks_total": len(st.results), "checks_ok": sum(1 for r in st.results if r.status == OK),
            "rules_total": len(rules),
            "failing": [r.name for r in st.results if r.status == FAIL],
            "sparkline": [round(r["valor"], 1) for r in reversed(spark)],
            "queued": self.queued.get(name),
        }

    def incident_dict(self, inc: Incident, full: bool = False) -> dict[str, Any]:
        spec = BY_NAME[inc.table]
        minutes = inc.minutes_open(self.now)
        data = {
            "id": inc.id, "table": inc.table, "table_title": spec.title, "title": inc.title,
            "severity": inc.severity, "severity_label": SEVERITY_LABEL[inc.severity], "status": inc.status,
            "opened_date": inc.opened[0], "opened_time": hhmm(inc.opened[1]), "owner": spec.owner,
            "owner_email": spec.owner_email, "minutes_open": minutes, "loads_failed": inc.loads_failed,
            "resolution": inc.resolution, "failing_checks": len(inc.checks), "simulated": inc.scenario is not None,
            "run_id": inc.run_id, "run_url": self._run_url(inc.run_id),
        }
        if full:
            since = (date.fromisoformat(inc.opened[0]) - timedelta(days=30)).isoformat()
            recurrence = self.store.scalar(
                "SELECT COUNT(DISTINCT fecha) FROM check_results WHERE tabla = ? AND estado = 'falla' "
                "AND fecha >= ? AND fecha < ?", (inc.table, since, inc.opened[0]))
            data |= {"checks": inc.checks, "timeline": inc.timeline, "consumers": list(spec.consumers),
                     "recurrence_30d": recurrence or 0}
        return data

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            tables = [self.table_summary(t.name) for t in TABLES]
            evaluated = [t for t in tables if t["score"] is not None]
            all_results = [r for st in self.tables.values() for r in st.results]
            resolved = [i for i in self.incidents.values() if i.resolved is not None]
            still_open = sorted((i for i in self.incidents.values() if i.resolved is None),
                                key=lambda i: i.opened, reverse=True)
            open_first = still_open + sorted(resolved, key=lambda i: i.resolved, reverse=True)
            series = self.store.query(
                "SELECT fecha, ROUND(valor, 1) AS score FROM metrics WHERE tabla = '_global' AND metrica = 'score' "
                "ORDER BY fecha DESC LIMIT 60")[::-1]
            mttr = [i.minutes_open(self.now) for i in resolved]
            return {
                "date": self.today.isoformat(), "weekday": DAYS_ES[self.today.weekday()], "time": hhmm(self.minute),
                "day_progress": round((self.minute - DAY_START) / (DAY_END - DAY_START), 3),
                "running": self.running, "speed": self.speed, "random_anomalies": self.random_anomalies,
                "kpis": {
                    "score": round(statistics.fmean(t["score"] for t in evaluated), 1) if evaluated else None,
                    "tables_total": len(tables),
                    "tables_arrived": sum(1 for t in tables if t["arrived_at"]),
                    "tables_ok": sum(1 for t in tables if t["status"] == "ok"),
                    "tables_missing": sum(1 for t in tables if t["status"] == "no_disponible"),
                    "open_incidents": sum(1 for i in self.incidents.values() if i.resolved is None),
                    "checks_today": len(all_results),
                    "checks_failed": sum(1 for r in all_results if r.status == FAIL),
                    "rows_today": sum(t["rows"] or 0 for t in tables),
                    "mttr_hours": round(statistics.fmean(mttr) / 60, 1) if mttr else None,
                },
                "tables": tables,
                "incidents": [self.incident_dict(i) for i in open_first[:40]],
                "events": list(self.events)[:40],
                "scenarios": [] if self.source else [
                    {"id": s.id, "title": s.title, "description": s.description, "table": s.table,
                     "queued": self.queued.get(s.table) == s.id} for s in SCENARIOS],
                "mode": "conector" if self.source else "simulacion",
                "source": None if not self.source else {
                    "name": self.source.name, "title": self.source.title, "description": self.source.description,
                    "path": str(self.source.path), "expected_at": hhmm(self.source.expected_at),
                    "url": self.source.project_url, **self.source_info},
                "score_series": series,
            }

    def table_detail(self, name: str) -> dict[str, Any]:
        with self.lock:
            spec = BY_NAME[name]
            st = self.tables[name]
            volume = self.store.query(
                """SELECT fecha,
                          MAX(CASE WHEN metrica = 'filas' THEN valor END)    AS filas,
                          MAX(CASE WHEN metrica = 'filas:lo' THEN valor END) AS lo,
                          MAX(CASE WHEN metrica = 'filas:hi' THEN valor END) AS hi
                   FROM metrics WHERE tabla = ? AND metrica IN ('filas', 'filas:lo', 'filas:hi')
                   GROUP BY fecha ORDER BY fecha DESC LIMIT 60""", (name,))[::-1]
            scores = self.store.query(
                "SELECT fecha, ROUND(valor, 1) AS score FROM metrics WHERE tabla = ? AND metrica = 'score' "
                "ORDER BY fecha DESC LIMIT 60", (name,))[::-1]
            days = [r["fecha"] for r in self.store.query(
                "SELECT DISTINCT fecha FROM check_results WHERE tabla = ? ORDER BY fecha DESC LIMIT 30", (name,))][::-1]
            heat: dict[str, dict] = {}
            if days:
                for r in self.store.query(
                        "SELECT fecha, check_id, nombre, estado FROM check_results WHERE tabla = ? AND fecha >= ?",
                        (name, days[0])):
                    row = heat.setdefault(r["check_id"], {"check_id": r["check_id"], "name": r["nombre"], "days": {}})
                    row["days"][r["fecha"]] = r["estado"]
            outliers = {}
            for rule in self.rules.for_table(name):
                if rule.type == "outlier":
                    key = f"rule:{rule.id}"
                    outliers[rule.id] = {"name": rule.describe(), "points": self.store.query(
                        """SELECT fecha,
                                  MAX(CASE WHEN metrica = ? THEN valor END) AS valor,
                                  MAX(CASE WHEN metrica = ? THEN valor END) AS lo,
                                  MAX(CASE WHEN metrica = ? THEN valor END) AS hi
                           FROM metrics WHERE tabla = ? AND metrica IN (?, ?, ?)
                           GROUP BY fecha ORDER BY fecha DESC LIMIT 60""",
                        (key, key + ":lo", key + ":hi", name, key, key + ":lo", key + ":hi"))[::-1]}
            return {
                "spec": spec.to_dict(), "summary": self.table_summary(name),
                "results": [r.to_dict() for r in st.results],
                "volume": volume, "scores": scores, "outliers": outliers,
                "heatmap": {"days": days, "rows": list(heat.values())},
                "profile": self.profile(name) if st.arrived is not None else [],
            }

    def profile(self, name: str) -> list[dict[str, Any]]:
        """Column profile of today's load: nulls, distinct values, min / max / mean."""
        spec = BY_NAME[name]
        iso = self.today.isoformat()
        parts = []
        for c in spec.columns:
            parts.append(f"SUM(CASE WHEN {c.name} IS NULL THEN 1 ELSE 0 END) AS \"{c.name}__nulos\"")
            parts.append(f"COUNT(DISTINCT {c.name}) AS \"{c.name}__distintos\"")
            if c.type in ("entero", "decimal"):
                parts += [f"MIN({c.name}) AS \"{c.name}__min\"", f"MAX({c.name}) AS \"{c.name}__max\"",
                          f"AVG({c.name}) AS \"{c.name}__prom\""]
        table = "t_" + spec.name  # identifier from the fixed schema, not from the request
        row = self.store.query(f"SELECT COUNT(*) AS n, {', '.join(parts)} FROM {table} WHERE _fecha_carga = ?",
                               (iso,))[0]
        n = row["n"] or 0
        out = []
        for c in spec.columns:
            nulls = row[f"{c.name}__nulos"] or 0
            out.append({
                "column": c.name, "type": c.type, "description": c.description,
                "null_pct": round(100 * nulls / n, 2) if n else None,
                "distinct": row[f"{c.name}__distintos"],
                "min": row.get(f"{c.name}__min"), "max": row.get(f"{c.name}__max"),
                "mean": round(row[f"{c.name}__prom"], 2) if row.get(f"{c.name}__prom") is not None else None,
            })
        return out

    def preview_rule(self, rule) -> dict[str, Any]:
        """Evaluate a rule against the latest available load of its table without saving it."""
        with self.lock:
            table = rule._table()
            latest = self.store.scalar(f"SELECT MAX(_fecha_carga) FROM {table}")
            if latest is None:
                raise ValueError("No hay cargas recientes de esta tabla para probar la regla")
            day = date.fromisoformat(latest)
            total = self.store.scalar(f"SELECT COUNT(*) FROM {table} WHERE _fecha_carga = ?", (latest,))
            if rule.type == "outlier":  # needs history: report today's value, evaluation starts with the next load
                value = self.store.scalar(rule.query()[0], {"fecha": latest}) or 0
                result = CheckResult(rule.table, "preview", rule.describe(), "regla", rule.severity, OK,
                                     f"Valor del {latest}: {value:,.2f}. Se comparará contra su histórico desde "
                                     "la próxima carga.", value=value, total_rows=total, sql=rule.sql())
            else:
                result = evaluate(rule, self.store, day, total)
            return {"date": latest, **result.to_dict()}
