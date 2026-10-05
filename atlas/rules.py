"""Quality rules: what "good data" means for each table, defined by the business.

Every row-level rule compiles to a SQL condition that selects the *bad* rows, so the
same rule can be shown to an analyst, run in the warehouse, or ported to dbt/Great
Expectations. Aggregate rules (outliers) compare today's value with its own history.
"""

from __future__ import annotations

import json
import re
import statistics
import threading
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from .store import Store
from .tables import BY_NAME

OK, WARN, FAIL, ERROR = "ok", "advertencia", "falla", "error"
SEVERITIES = ("critica", "alta", "media", "baja")
SEVERITY_WEIGHT = {"critica": 4, "alta": 3, "media": 2, "baja": 1}
SEVERITY_LABEL = {"critica": "Crítica", "alta": "Alta", "media": "Media", "baja": "Baja"}
OPERATORS = ("<=", "<", ">=", ">", "=", "!=")
AGGREGATES = {"suma": "SUM", "promedio": "AVG", "conteo": "COUNT", "maximo": "MAX", "minimo": "MIN"}
RULE_TYPES = {
    "no_nulos": "Sin vacíos",
    "unico": "Sin duplicados",
    "rango": "Dentro de un rango",
    "valores_permitidos": "Valores permitidos",
    "comparacion": "Comparación entre columnas",
    "fecha_del_dia": "Datos del día",
    "outlier": "Valor atípico vs. histórico",
    "sql": "Condición SQL personalizada",
}
FORBIDDEN_SQL = re.compile(r";|--|/\*|\b(insert|update|delete|drop|alter|create|attach|detach|pragma|replace|"
                           r"vacuum|reindex|trigger)\b", re.IGNORECASE)


@dataclass
class CheckResult:
    table: str
    check_id: str
    name: str
    kind: str  # monitor | regla
    severity: str
    status: str
    message: str
    value: float | None = None
    threshold: str = ""
    failing_rows: int = 0
    total_rows: int = 0
    sql: str | None = None
    rule_type: str = ""
    examples: list[dict] = field(default_factory=list)
    series: dict | None = None

    def to_dict(self, examples: bool = True) -> dict[str, Any]:
        data = asdict(self)
        if not examples:
            data.pop("examples")
            data.pop("series")
        data["severity_label"] = SEVERITY_LABEL[self.severity]
        return data


@dataclass
class Rule:
    id: str
    table: str
    type: str
    severity: str
    params: dict[str, Any]
    enabled: bool = True
    note: str = ""
    author: str = "sistema"

    # --------------------------------------------------------------- display
    @property
    def column(self) -> str | None:
        return self.params.get("column")

    def describe(self) -> str:
        p = self.params
        col = p.get("column")
        if self.type == "no_nulos":
            tol = p.get("max_pct", 0)
            return f"{col} no puede estar vacío" + (f" (tolerancia {tol}%)" if tol else "")
        if self.type == "unico":
            return f"{' + '.join(p['columns'])} no se puede repetir"
        if self.type == "rango":
            lo, hi = p.get("min"), p.get("max")
            if lo is not None and hi is not None:
                return f"{col} entre {_n(lo)} y {_n(hi)}"
            return f"{col} ≥ {_n(lo)}" if lo is not None else f"{col} ≤ {_n(hi)}"
        if self.type == "valores_permitidos":
            return f"{col} solo puede ser: {', '.join(map(str, p['values']))}"
        if self.type == "comparacion":
            return f"{col} {p['operator']} {p['other_column']}"
        if self.type == "fecha_del_dia":
            return f"{col} debe ser la fecha del día de carga"
        if self.type == "outlier":
            scope = "vs. mismo día de la semana" if p.get("same_weekday", True) else "vs. últimos 14 días"
            return f"{p['aggregate'].capitalize()} de {col} sin valores atípicos (±{float(p.get('z', 3)):g}σ {scope})"
        if self.type == "sql":
            return f"Registros que cumplen: {p['condition']}"
        return self.type

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "description": self.describe(), "type_label": RULE_TYPES[self.type],
                "severity_label": SEVERITY_LABEL[self.severity]}

    # ------------------------------------------------------------ validation
    def validate(self) -> None:
        spec = BY_NAME.get(self.table)
        if spec is None:
            raise ValueError(f"La tabla '{self.table}' no existe")
        if self.type not in RULE_TYPES:
            raise ValueError(f"Tipo de regla desconocido: {self.type}")
        if self.severity not in SEVERITIES:
            raise ValueError(f"Severidad inválida: {self.severity}")
        cols = {c.name: c for c in spec.columns}
        p = self.params

        def need_column(key: str = "column", numeric: bool = False) -> None:
            name = p.get(key)
            if name not in cols:
                raise ValueError(f"La columna '{name}' no existe en {self.table}")
            if numeric and cols[name].type not in ("entero", "decimal"):
                raise ValueError(f"La columna '{name}' no es numérica")

        if len(str(self.params)) > 2000 or len(self.note) > 300:
            raise ValueError("La regla es demasiado larga")
        if self.type == "no_nulos":
            need_column()
            p["max_pct"] = float(p.get("max_pct") or 0)
        elif self.type == "unico":
            if not p.get("columns"):
                raise ValueError("Indica al menos una columna")
            for c in p["columns"]:
                if c not in cols:
                    raise ValueError(f"La columna '{c}' no existe en {self.table}")
        elif self.type == "rango":
            need_column(numeric=True)
            for k in ("min", "max"):
                if p.get(k) in ("", None):
                    p[k] = None
                else:
                    p[k] = float(p[k])
            if p["min"] is None and p["max"] is None:
                raise ValueError("Indica un mínimo, un máximo o ambos")
        elif self.type == "valores_permitidos":
            need_column()
            values = p.get("values")
            if isinstance(values, str):
                values = [v.strip() for v in values.split(",") if v.strip()]
            if not values:
                raise ValueError("Indica al menos un valor permitido")
            if len(values) > 100 or any(len(str(v)) > 100 for v in values):
                raise ValueError("Máximo 100 valores permitidos, de hasta 100 caracteres cada uno")
            p["values"] = values
        elif self.type == "comparacion":
            need_column(numeric=False)
            need_column("other_column")
            if p.get("operator") not in OPERATORS:
                raise ValueError(f"Operador inválido; usa uno de {', '.join(OPERATORS)}")
        elif self.type == "fecha_del_dia":
            need_column()
        elif self.type == "outlier":
            if p.get("aggregate") not in AGGREGATES:
                raise ValueError(f"Agregación inválida; usa {', '.join(AGGREGATES)}")
            if p["aggregate"] == "conteo":
                p.setdefault("column", spec.columns[0].name)
            need_column(numeric=p["aggregate"] != "conteo")
            p["z"] = float(p.get("z") or 3)
            p["same_weekday"] = bool(p.get("same_weekday", True))
        elif self.type == "sql":
            cond = (p.get("condition") or "").strip()
            if not cond:
                raise ValueError("Escribe la condición SQL que identifica los registros malos")
            if len(cond) > 500:
                raise ValueError("La condición SQL no puede pasar de 500 caracteres")
            if FORBIDDEN_SQL.search(cond):
                raise ValueError("La condición solo puede leer datos (sin ;, comentarios ni comandos de escritura)")
            p["condition"] = cond

    # ------------------------------------------------------------- SQL logic
    # Identifiers (table, columns, operator, aggregate) are always taken from the fixed schema and
    # whitelists, never from the incoming text; literal values travel as bound parameters.
    def _table(self) -> str:
        return "t_" + next(t.name for t in BY_NAME.values() if t.name == self.table)

    def _col(self, name: str | None) -> str:
        for c in BY_NAME[self.table].columns:
            if c.name == name:
                return c.name
        raise ValueError(f"La columna '{name}' no existe en {self.table}")

    def bad_rows_condition(self) -> tuple[str, dict[str, Any]] | None:
        """SQL condition selecting the *bad* rows, plus its bound parameters."""
        p = self.params
        if self.type == "sql":
            return f"({p['condition']})", {}  # user SQL by design: runs read-only and time-boxed
        col = self._col(p.get("column"))
        if self.type == "no_nulos":
            return f"{col} IS NULL OR TRIM(CAST({col} AS TEXT)) = ''", {}
        if self.type == "rango":
            parts, binds = [], {}
            if p.get("min") is not None:
                parts.append(f"{col} < :minimo")
                binds["minimo"] = float(p["min"])
            if p.get("max") is not None:
                parts.append(f"{col} > :maximo")
                binds["maximo"] = float(p["max"])
            return f"{col} IS NOT NULL AND ({' OR '.join(parts)})", binds
        if self.type == "valores_permitidos":
            binds = {f"v{i}": str(v) for i, v in enumerate(p["values"])}
            return f"{col} IS NOT NULL AND {col} NOT IN ({', '.join(':' + k for k in binds)})", binds
        if self.type == "comparacion":
            other = self._col(p["other_column"])
            op = next(o for o in OPERATORS if o == p["operator"])
            return f"{col} IS NOT NULL AND {other} IS NOT NULL AND NOT ({col} {op} {other})", {}
        if self.type == "fecha_del_dia":
            return f"{col} IS NULL OR {col} <> :fecha", {}
        return None

    def query(self) -> tuple[str, dict[str, Any]]:
        """Executable SQL for this rule (bad rows, duplicate groups or the daily aggregate)."""
        table = self._table()
        if self.type == "unico":
            cols = ", ".join(self._col(c) for c in self.params["columns"])
            return (f"SELECT {cols}, COUNT(*) AS veces FROM {table}\nWHERE _fecha_carga = :fecha\n"
                    f"GROUP BY {cols} HAVING COUNT(*) > 1"), {}
        if self.type == "outlier":
            agg = AGGREGATES[next(a for a in AGGREGATES if a == self.params["aggregate"])]
            target = "*" if agg == "COUNT" else self._col(self.params["column"])
            return f"SELECT {agg}({target}) FROM {table}\nWHERE _fecha_carga = :fecha", {}
        cond, binds = self.bad_rows_condition()
        return f"SELECT * FROM {table}\nWHERE _fecha_carga = :fecha\n  AND ({cond})", binds

    def sql(self) -> str:
        """Readable SQL for people: bound values shown inline (never executed)."""
        text, binds = self.query()
        for key in sorted(binds, key=len, reverse=True):
            value = binds[key]
            literal = f"{value:g}" if isinstance(value, float) else "'" + str(value).replace("'", "''") + "'"
            text = text.replace(f":{key}", literal)
        return text


def _n(v: float | None) -> str:
    """Formato colombiano: 48.159.795.968 · 4,18 · -7,73."""
    if v is None:
        return "—"
    if float(v).is_integer() or abs(v) >= 1000:
        return f"{v:,.0f}".replace(",", ".")
    return f"{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


# --------------------------------------------------------------------- evaluation
def evaluate(rule: Rule, store: Store, day: date, total_rows: int) -> CheckResult:
    iso = day.isoformat()
    base = CheckResult(rule.table, rule.id, rule.describe(), "regla", rule.severity, OK, "",
                       total_rows=total_rows, sql=rule.sql(), rule_type=rule.type)
    try:
        if rule.type == "outlier":
            return _evaluate_outlier(rule, store, day, base)
        if rule.type == "unico":
            q, _ = rule.query()
            groups = store.query(q + " ORDER BY veces DESC", {"fecha": iso})
            extra = sum(g["veces"] - 1 for g in groups)
            base.failing_rows = extra
            base.examples = groups[:5]
            base.value = extra
            base.threshold = "0 repetidos"
            base.status = FAIL if extra else OK
            base.message = (f"{extra:,} registros repetidos en {len(groups):,} valores de "
                            f"{' + '.join(rule.params['columns'])}" if extra else "Sin duplicados").replace(",", ".")
            return base

        cond, binds = rule.bad_rows_condition()
        binds = {"fecha": iso, **binds}
        table = rule._table()
        run = store.query_readonly if rule.type == "sql" else store.query
        bad = run(f"SELECT COUNT(*) AS n FROM {table} WHERE _fecha_carga = :fecha AND ({cond})", binds)[0]["n"]
        base.failing_rows = bad
        pct = 100 * bad / total_rows if total_rows else 0.0
        base.value = round(pct, 3)
        tolerance = float(rule.params.get("max_pct", 0) or 0)
        base.threshold = f"≤ {tolerance:g}% de registros"
        base.status = FAIL if bad and pct > tolerance else OK
        if bad:
            cols = BY_NAME[rule.table].column_names
            base.examples = run(f"SELECT {', '.join(cols)} FROM {table} WHERE _fecha_carga = :fecha AND ({cond}) "
                                "LIMIT 5", binds)
        base.message = (f"{bad:,} de {total_rows:,} registros incumplen ({pct:.2f}%)".replace(",", ".")
                        if bad else "Todos los registros cumplen")
        return base
    except Exception as exc:  # a broken user rule must never stop the monitor
        base.status = ERROR
        base.message = f"No se pudo evaluar la regla: {exc}"
        return base


def baseline(history: list[tuple[str, float]], day: date, same_weekday: bool,
             min_spread: float = 0.0) -> tuple[float, float] | None:
    """What 'normal' looks like for a metric: (center, spread).

    History only contains days that passed (anomalies never teach the baseline), so the
    sample standard deviation is safe to use; the median keeps the center stable and
    the relative floor avoids flagging tiny wiggles on very stable series.
    """
    if same_weekday:
        values = [v for d, v in history if date.fromisoformat(d).weekday() == day.weekday()][-6:]
    else:
        values = [v for _, v in history][-14:]
    if len(values) < 4:
        return None
    center = statistics.median(values)
    mad = statistics.median(abs(v - center) for v in values)
    spread = max(statistics.stdev(values), 1.4826 * mad, abs(center) * 0.05, min_spread, 1e-9)
    return center, spread


def _evaluate_outlier(rule: Rule, store: Store, day: date, base: CheckResult) -> CheckResult:
    iso = day.isoformat()
    p = rule.params
    q, _ = rule.query()
    value = store.scalar(q, {"fecha": iso})
    value = float(value or 0)
    key = f"rule:{rule.id}"
    stats = baseline(store.metric_history(rule.table, key + ":base", iso, 60), day, p.get("same_weekday", True))
    base.value = round(value, 4)
    store.put_metric(iso, rule.table, key, value)
    if stats is None:
        base.message = f"{p['aggregate'].capitalize()} hoy: {_n(value)} (aún sin historia suficiente)"
        base.threshold = "requiere ≥ 4 días comparables"
        store.put_metric(iso, rule.table, key + ":base", value)
        return base
    mean, std = stats
    z = p.get("z", 3)
    lo, hi = mean - z * std, mean + z * std
    score = (value - mean) / std
    store.put_metric(iso, rule.table, key + ":lo", lo)
    store.put_metric(iso, rule.table, key + ":hi", hi)
    base.threshold = f"entre {_n(lo)} y {_n(hi)}"
    if abs(score) > z:
        base.status = FAIL
        direction = "por encima" if score > 0 else "por debajo"
        base.message = (f"{p['aggregate'].capitalize()} de {p['column']} = {_n(value)}, {abs(score):.1f}σ {direction} "
                        f"de lo normal (promedio {_n(mean)})")
    else:
        store.put_metric(iso, rule.table, key + ":base", value)  # only normal days teach the baseline
        base.message = f"{p['aggregate'].capitalize()} de {p['column']} = {_n(value)} (normal: {_n(lo)} – {_n(hi)})"
    return base


# --------------------------------------------------------------------- persistence
DEFAULT_RULES: list[dict[str, Any]] = [
    # cartera_creditos
    {"table": "cartera_creditos", "type": "unico", "severity": "critica", "params": {"columns": ["id_credito"]},
     "note": "Un crédito duplicado duplica saldos y provisiones."},
    {"table": "cartera_creditos", "type": "fecha_del_dia", "severity": "critica", "params": {"column": "fecha_corte"},
     "note": "Detecta que se recargó un archivo viejo."},
    {"table": "cartera_creditos", "type": "comparacion", "severity": "alta",
     "params": {"column": "saldo_capital", "operator": "<=", "other_column": "monto_desembolsado"}},
    {"table": "cartera_creditos", "type": "rango", "severity": "alta",
     "params": {"column": "tasa_interes_ea", "min": 0, "max": 60}, "note": "Tope de usura de referencia."},
    {"table": "cartera_creditos", "type": "rango", "severity": "media", "params": {"column": "dias_mora", "min": 0}},
    {"table": "cartera_creditos", "type": "valores_permitidos", "severity": "media",
     "params": {"column": "calificacion", "values": ["A", "B", "C", "D", "E"]}},
    {"table": "cartera_creditos", "type": "no_nulos", "severity": "alta", "params": {"column": "id_cliente"}},
    {"table": "cartera_creditos", "type": "outlier", "severity": "media",
     "params": {"column": "saldo_capital", "aggregate": "suma", "z": 4, "same_weekday": False}},
    # pagos
    {"table": "pagos", "type": "unico", "severity": "critica", "params": {"columns": ["id_pago"]}},
    {"table": "pagos", "type": "rango", "severity": "alta", "params": {"column": "valor_pago", "min": 1}},
    {"table": "pagos", "type": "valores_permitidos", "severity": "media",
     "params": {"column": "canal", "values": ["app", "pse", "oficina", "corresponsal"]}},
    {"table": "pagos", "type": "no_nulos", "severity": "alta", "params": {"column": "id_credito"}},
    {"table": "pagos", "type": "outlier", "severity": "media",
     "params": {"column": "valor_pago", "aggregate": "suma", "z": 4, "same_weekday": True}},
    # desembolsos
    {"table": "desembolsos", "type": "unico", "severity": "critica", "params": {"columns": ["id_desembolso"]}},
    {"table": "desembolsos", "type": "outlier", "severity": "alta",
     "params": {"column": "valor_desembolso", "aggregate": "suma", "z": 4, "same_weekday": True}},
    {"table": "desembolsos", "type": "outlier", "severity": "media",
     "params": {"column": "valor_desembolso", "aggregate": "promedio", "z": 4, "same_weekday": False}},
    # clientes
    {"table": "clientes", "type": "no_nulos", "severity": "critica", "params": {"column": "numero_documento"},
     "note": "Requisito de conocimiento del cliente (SARLAFT)."},
    {"table": "clientes", "type": "unico", "severity": "alta", "params": {"columns": ["tipo_documento", "numero_documento"]}},
    {"table": "clientes", "type": "valores_permitidos", "severity": "media",
     "params": {"column": "tipo_documento", "values": ["CC", "CE", "NIT"]}},
    {"table": "clientes", "type": "rango", "severity": "baja", "params": {"column": "ingresos_mensuales", "min": 0}},
    # indicadores_cartera
    {"table": "indicadores_cartera", "type": "rango", "severity": "critica",
     "params": {"column": "tasa_mora", "min": 0, "max": 100}, "note": "Una tasa de mora nunca puede ser negativa."},
    {"table": "indicadores_cartera", "type": "unico", "severity": "alta", "params": {"columns": ["fecha_corte", "producto"]}},
    {"table": "indicadores_cartera", "type": "comparacion", "severity": "alta",
     "params": {"column": "saldo_vencido", "operator": "<=", "other_column": "saldo_total"}},
    {"table": "indicadores_cartera", "type": "outlier", "severity": "alta",
     "params": {"column": "tasa_mora", "aggregate": "promedio", "z": 4, "same_weekday": False}},
    # tasas_mercado
    {"table": "tasas_mercado", "type": "unico", "severity": "alta", "params": {"columns": ["fecha", "indicador"]}},
    {"table": "tasas_mercado", "type": "sql", "severity": "critica",
     "params": {"condition": "indicador = 'TRM' AND (valor < 2500 OR valor > 7000)"},
     "note": "Rango de razonabilidad de la TRM."},
]


class RuleStore:
    def __init__(self, path: str | None) -> None:
        self.path = Path(path) if path else None
        self.lock = threading.RLock()
        self.rules: dict[str, Rule] = {}
        if self.path and self.path.exists():
            for raw in json.loads(self.path.read_text(encoding="utf-8")):
                rule = Rule(**raw)
                self.rules[rule.id] = rule
        else:
            for i, raw in enumerate(DEFAULT_RULES, 1):
                rule = Rule(id=f"R{i:03d}", **raw)
                rule.validate()
                self.rules[rule.id] = rule
            self.save()

    def save(self) -> None:
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = [asdict(r) for r in self.rules.values()]
        self.path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def for_table(self, table: str, enabled_only: bool = True) -> list[Rule]:
        return [r for r in self.rules.values() if r.table == table and (r.enabled or not enabled_only)]

    def next_id(self) -> str:
        nums = [int(r.id[1:]) for r in self.rules.values() if r.id[1:].isdigit()]
        return f"R{max(nums, default=0) + 1:03d}"

    def add(self, rule: Rule) -> Rule:
        with self.lock:
            rule.validate()
            self.rules[rule.id] = rule
            self.save()
            return rule

    def update(self, rule_id: str, changes: dict[str, Any]) -> Rule:
        with self.lock:
            current = asdict(self.rules[rule_id])
            current.update({k: v for k, v in changes.items() if k in current and k != "id"})
            rule = Rule(**current)
            rule.validate()
            self.rules[rule_id] = rule
            self.save()
            return rule

    def delete(self, rule_id: str) -> None:
        with self.lock:
            del self.rules[rule_id]
            self.save()
