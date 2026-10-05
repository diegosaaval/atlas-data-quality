"""Automatic monitors: checks every table gets without anyone writing a rule.

* disponibilidad — did today's load arrive, and on time?
* volumen        — is the row count normal for this day of the week?
* estructura     — did the load bring the expected columns?
"""

from __future__ import annotations

import math
import statistics
from datetime import date

from .rules import FAIL, OK, WARN, CheckResult, baseline
from .store import Store
from .tables import DAYS_ES, TableSpec

GRACE_MINUTES = 60


def hhmm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def availability(spec: TableSpec, arrived: int | None, now: int) -> CheckResult:
    check = CheckResult(spec.name, "disponibilidad", "Disponibilidad de la tabla del día", "monitor", "critica", OK, "", rule_type="disponibilidad",
                        threshold=f"antes de {hhmm(spec.expected_at + GRACE_MINUTES)}")
    late = (arrived if arrived is not None else now) - spec.expected_at
    if arrived is None:
        check.status = FAIL
        check.value = late
        check.message = (f"La tabla no ha llegado. Se esperaba a las {spec.expected_hhmm} "
                         f"({late} min de retraso).")
    elif late > GRACE_MINUTES:
        check.status = WARN
        check.value = late
        check.message = f"Llegó a las {hhmm(arrived)}, {late} min después de lo acordado ({spec.expected_hhmm})."
    else:
        check.value = max(0, late)
        check.message = f"Llegó a las {hhmm(arrived)} (esperada {spec.expected_hhmm})."
    return check


SPARSE_ROWS = 30  # por debajo de esto, contar 0, 3 o 13 eventos en un día es variación normal


def volume(spec: TableSpec, rows: int, store: Store, day: date) -> CheckResult:
    iso = day.isoformat()
    same_weekday = spec.load_type == "incremental"
    check = CheckResult(spec.name, "volumen", "Volumen de registros", "monitor", "alta", OK, "", rule_type="volumen",
                        value=rows, total_rows=rows)
    store.put_metric(iso, spec.name, "filas", rows)
    history = store.metric_history(spec.name, "filas:base", iso, 60)
    typical = statistics.median([v for _, v in history]) if history else 0
    if typical < SPARSE_ROWS:  # pocos eventos al día (p. ej. contracargos): un solo día de la semana es muy poca muestra
        same_weekday = False
    # Counts fluctuate at least like a Poisson process: never use a band tighter than sqrt(n).
    stats = baseline(history, day, same_weekday, min_spread=max(1.0, math.sqrt(typical)))
    if stats is None:
        check.message = f"{rows:,} registros (aún sin historia suficiente)".replace(",", ".")
        check.threshold = "requiere ≥ 4 días comparables"
        store.put_metric(iso, spec.name, "filas:base", rows)
        return check
    mean, std = stats
    z = 3.5
    lo, hi = max(0.0, mean - z * std), mean + z * std
    if typical < SPARSE_ROWS:
        # Con pocos eventos al día el conteo es ruidoso: solo un salto grande es señal. Las filas
        # repetidas de una tabla así las detecta la regla de unicidad, no el volumen.
        lo, hi = 0.0, max(hi, 3 * mean, SPARSE_ROWS)
    store.put_metric(iso, spec.name, "filas:lo", lo)
    store.put_metric(iso, spec.name, "filas:hi", hi)
    store.put_metric(iso, spec.name, "filas:esperado", mean)
    scope = f"un {DAYS_ES[day.weekday()]}" if same_weekday else "estos días"
    check.threshold = f"entre {lo:,.0f} y {hi:,.0f}".replace(",", ".")
    fmt = lambda v: f"{v:,.0f}".replace(",", ".")  # noqa: E731
    if rows == 0 and lo > 0:
        check.status = FAIL
        check.message = f"La carga llegó vacía: 0 registros. Lo normal {scope} es ~{fmt(mean)}."
    elif rows < lo:
        check.status = FAIL
        check.message = (f"Llegaron {fmt(rows)} registros, {100 * (1 - rows / max(mean, 1)):.0f}% menos de lo normal {scope} "
                         f"(~{fmt(mean)}). Posible carga incompleta.")
    elif rows > hi:
        check.status = FAIL
        times = f"{rows / mean:.1f} veces lo normal" if mean >= 1 else "muy por encima de lo normal"
        check.message = f"Llegaron {fmt(rows)} registros, {times} {scope} (~{fmt(mean)}). Posible duplicación."
    else:
        check.message = f"{fmt(rows)} registros (normal {scope}: {fmt(lo)} – {fmt(hi)})."
        store.put_metric(iso, spec.name, "filas:base", rows)
    return check


def structure(spec: TableSpec, received: list[str]) -> CheckResult:
    expected = set(spec.column_names)
    got = set(received)
    missing, extra = sorted(expected - got), sorted(got - expected)
    check = CheckResult(spec.name, "estructura", "Estructura de columnas", "monitor", "alta", OK, "", rule_type="estructura",
                        value=len(missing) + len(extra), threshold=f"{len(expected)} columnas")
    if missing:
        check.status = FAIL
        check.message = f"Faltan columnas: {', '.join(missing)}."
    elif extra:
        check.status = WARN
        check.message = f"Columnas nuevas no esperadas: {', '.join(extra)}."
    else:
        check.message = f"Las {len(expected)} columnas esperadas están presentes."
    return check
