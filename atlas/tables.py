"""Monitored tables and the synthetic bank that "loads" them every morning.

ATLAS does not move data: it watches the tables another team (IT / data integration)
loads every day. This module plays that other team — it produces realistic daily loads
for six banking tables, and can be told to produce a bad load (duplicates, empty file,
yesterday's file again...) so the monitor has something to catch.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

PRODUCTS = {  # product: (min amount, max amount, base annual rate %)
    "consumo": (3_000_000, 60_000_000, 24.0),
    "vivienda": (80_000_000, 450_000_000, 12.5),
    "libranza": (5_000_000, 90_000_000, 16.5),
    "tarjeta": (1_000_000, 25_000_000, 29.0),
    "microcredito": (1_000_000, 18_000_000, 34.0),
}
OFFICES = ("Medellín - El Poblado", "Medellín - Centro", "Bogotá - Chicó", "Bogotá - Chapinero", "Cali - Granada",
           "Barranquilla - Norte", "Bucaramanga - Cabecera", "Pereira - Circunvalar", "Cartagena - Bocagrande")
FIRST = ("Ana", "Luis", "Camila", "Andrés", "Valentina", "Santiago", "María", "Juan", "Laura", "Diego",
         "Sofía", "Mateo", "Isabela", "Carlos", "Daniela", "Felipe")
LAST = ("Gómez", "Restrepo", "Rodríguez", "Martínez", "López", "Ramírez", "Torres", "Vélez", "Ospina",
        "Zapata", "Cárdenas", "Moreno")
WEEKDAY_FACTOR = (1.15, 1.0, 1.0, 1.0, 1.05, 0.55, 0.3)  # Mon..Sun
DAYS_ES = ("lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo")


@dataclass(frozen=True)
class Column:
    name: str
    type: str  # texto | entero | decimal | fecha
    description: str


@dataclass(frozen=True)
class TableSpec:
    name: str
    title: str
    description: str
    owner: str
    owner_email: str
    expected_at: int  # minutes after midnight
    load_type: str  # snapshot | incremental
    date_column: str
    columns: tuple[Column, ...]
    consumers: tuple[str, ...]

    @property
    def expected_hhmm(self) -> str:
        return f"{self.expected_at // 60:02d}:{self.expected_at % 60:02d}"

    @property
    def column_names(self) -> list[str]:
        return [c.name for c in self.columns]

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name, "title": self.title, "description": self.description, "owner": self.owner,
            "owner_email": self.owner_email, "expected_at": self.expected_hhmm, "load_type": self.load_type,
            "date_column": self.date_column, "consumers": list(self.consumers),
            "columns": [{"name": c.name, "type": c.type, "description": c.description} for c in self.columns],
        }


C = Column
BANK_TABLES: tuple[TableSpec, ...] = (
    TableSpec(
        "clientes", "Clientes (novedades)", "Clientes vinculados en el día.",
        "Gobierno de Datos · Clientes", "gobierno.clientes@banco.example", 6 * 60, "incremental", "fecha_vinculacion",
        (C("id_cliente", "texto", "Identificador interno"), C("tipo_documento", "texto", "CC, CE o NIT"),
         C("numero_documento", "texto", "Número de documento"), C("nombre_cliente", "texto", "Nombre completo"),
         C("segmento", "texto", "personas, pyme o empresarial"), C("ingresos_mensuales", "decimal", "COP"),
         C("fecha_vinculacion", "fecha", "Fecha de vinculación")),
        ("Conozca a su cliente (SARLAFT)", "CRM comercial"),
    ),
    TableSpec(
        "cartera_creditos", "Cartera de créditos", "Foto diaria de todos los créditos vigentes.",
        "Riesgo de Crédito", "riesgo.credito@banco.example", 6 * 60 + 30, "snapshot", "fecha_corte",
        (C("id_credito", "texto", "Identificador del crédito"), C("id_cliente", "texto", "Cliente titular"),
         C("producto", "texto", "Línea de crédito"), C("fecha_corte", "fecha", "Fecha de la foto"),
         C("monto_desembolsado", "decimal", "Monto original COP"), C("saldo_capital", "decimal", "Saldo COP"),
         C("tasa_interes_ea", "decimal", "Tasa efectiva anual %"), C("dias_mora", "entero", "Días de atraso"),
         C("calificacion", "texto", "Calificación de riesgo A–E")),
        ("Cálculo de provisiones", "Reporte regulatorio de cartera", "Tablero de riesgo"),
    ),
    TableSpec(
        "pagos", "Pagos recibidos", "Pagos de cuotas recibidos en el día por todos los canales.",
        "Recaudo y Cobranzas", "recaudo@banco.example", 7 * 60, "incremental", "fecha_pago",
        (C("id_pago", "texto", "Identificador del pago"), C("id_credito", "texto", "Crédito al que abona"),
         C("fecha_pago", "fecha", "Fecha del pago"), C("valor_pago", "decimal", "COP"),
         C("canal", "texto", "app, pse, oficina o corresponsal"), C("estado", "texto", "aplicado o reversado")),
        ("Conciliación contable", "Gestión de cobranza"),
    ),
    TableSpec(
        "tasas_mercado", "Tasas de mercado", "TRM, IBR y DTF publicadas para el día.",
        "Tesorería", "tesoreria@banco.example", 7 * 60 + 30, "snapshot", "fecha",
        (C("fecha", "fecha", "Fecha de publicación"), C("indicador", "texto", "TRM, IBR_1M o DTF"),
         C("valor", "decimal", "Valor del indicador")),
        ("Valoración de portafolio", "Créditos en dólares"),
    ),
    TableSpec(
        "desembolsos", "Desembolsos", "Créditos nuevos desembolsados en el día.",
        "Originación de Crédito", "originacion@banco.example", 8 * 60, "incremental", "fecha_desembolso",
        (C("id_desembolso", "texto", "Identificador"), C("id_credito", "texto", "Crédito creado"),
         C("id_cliente", "texto", "Cliente"), C("fecha_desembolso", "fecha", "Fecha"),
         C("producto", "texto", "Línea de crédito"), C("valor_desembolso", "decimal", "COP"),
         C("oficina", "texto", "Oficina que desembolsa")),
        ("Metas comerciales", "Reporte de colocación"),
    ),
    TableSpec(
        "indicadores_cartera", "Indicadores de cartera", "Saldo, cartera vencida y tasa de mora por producto.",
        "Riesgo de Crédito", "riesgo.credito@banco.example", 8 * 60 + 30, "snapshot", "fecha_corte",
        (C("fecha_corte", "fecha", "Fecha de corte"), C("producto", "texto", "Línea de crédito"),
         C("numero_creditos", "entero", "Créditos vigentes"), C("saldo_total", "decimal", "COP"),
         C("saldo_vencido", "decimal", "Saldo con más de 30 días de mora"),
         C("tasa_mora", "decimal", "Cartera vencida / saldo total, en %")),
        ("Comité de riesgo", "Junta directiva", "Indicadores para el regulador"),
    ),
)
# Catálogo activo. Por defecto, el banco de demo; un conector lo reemplaza con sus propias tablas.
# Se modifica en sitio para que todos los módulos que lo importaron vean el cambio.
TABLES: list[TableSpec] = list(BANK_TABLES)
BY_NAME: dict[str, TableSpec] = {t.name: t for t in TABLES}


def use_tables(specs: list[TableSpec] | tuple[TableSpec, ...]) -> None:
    """Cambia el catálogo activo de tablas monitoreadas."""
    TABLES[:] = list(specs)
    BY_NAME.clear()
    BY_NAME.update({t.name: t for t in TABLES})


@dataclass(frozen=True)
class Scenario:
    id: str
    title: str
    description: str
    table: str


SCENARIOS: tuple[Scenario, ...] = (
    Scenario("no_llega", "La tabla de pagos no llega",
             "El proceso de carga de TI no corre y la tabla del día nunca aparece.", "pagos"),
    Scenario("llega_tarde", "Desembolsos llega 2 horas tarde",
             "La tabla llega, pero mucho después de la hora acordada.", "desembolsos"),
    Scenario("ingesta_borrada", "Carga de pagos vacía",
             "La carga corre pero deja la tabla con 0 registros (ingesta borrada o archivo vacío).", "pagos"),
    Scenario("carga_parcial", "Carga parcial de pagos",
             "Solo llega una parte del archivo: ~35% de los registros esperados.", "pagos"),
    Scenario("carga_duplicada", "Cartera cargada dos veces",
             "El archivo se procesa dos veces y cada crédito aparece duplicado.", "cartera_creditos"),
    Scenario("archivo_de_ayer", "Se recarga el archivo de ayer",
             "La carga de cartera trae la foto del día anterior (fecha_corte de ayer).", "cartera_creditos"),
    Scenario("saldo_mayor_monto", "Saldos mayores al monto desembolsado",
             "Un error de cálculo deja saldos de capital por encima del monto original.", "cartera_creditos"),
    Scenario("tasa_mora_negativa", "Tasa de mora negativa",
             "Un error en el cálculo produce una tasa de mora negativa en un producto.", "indicadores_cartera"),
    Scenario("pico_desembolsos", "Pico anómalo de desembolsos",
             "Los valores desembolsados se multiplican por un error de unidades (pesos vs. miles).", "desembolsos"),
    Scenario("nulos_documento", "Clientes sin número de documento",
             "30% de los clientes nuevos llegan sin número de documento.", "clientes"),
    Scenario("canal_invalido", "Canal de pago desconocido",
             "Un canal nuevo no homologado ('DESCONOCIDO') aparece en los pagos.", "pagos"),
    Scenario("columna_eliminada", "Desaparece una columna",
             "Un cambio en el sistema fuente elimina la columna 'canal' de pagos.", "pagos"),
    Scenario("trm_atipica", "TRM con valor atípico",
             "La TRM llega con un valor fuera de rango por un error de digitación.", "tasas_mercado"),
)
SCENARIOS_BY_ID = {s.id: s for s in SCENARIOS}


@dataclass
class Load:
    table: str
    rows: list[dict[str, Any]]
    columns: list[str]
    arrives_at: int | None  # minutes after midnight; None = never arrives


@dataclass
class Credit:
    id: str
    client: str
    product: str
    amount: float
    balance: float
    rate: float
    days_late: int


@dataclass
class SyntheticBank:
    seed: int
    rng: random.Random = field(init=False)
    credits: list[Credit] = field(default_factory=list)
    clients: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.rng = random.Random(self.seed)
        self._client_seq = 0
        self._credit_seq = 0
        self._doc_seq = 0
        self._trm, self._ibr, self._dtf = 4100.0, 9.25, 9.6
        self._yesterday: date | None = None
        for _ in range(900):
            self._new_client()
        for _ in range(1800):
            self._new_credit(self.rng.choice(self.clients))
        for c in self.credits:  # start from a steady-state delinquency mix
            if self.rng.random() < 0.09:
                c.days_late = self.rng.randint(1, 120)

    # ----------------------------------------------------------------- helpers
    def _new_client(self) -> str:
        self._client_seq += 1
        cid = f"CL-{self._client_seq:06d}"
        self.clients.append(cid)
        return cid

    def _new_credit(self, client: str, product: str | None = None, amount: float | None = None) -> Credit:
        rng = self.rng
        self._credit_seq += 1
        product = product or rng.choices(list(PRODUCTS), (35, 10, 25, 22, 8))[0]
        lo, hi, rate = PRODUCTS[product]
        typical = math.sqrt(lo * hi)
        amount = amount or round(min(hi, max(lo, rng.lognormvariate(math.log(typical), 0.45))), -3)
        credit = Credit(f"CR-{self._credit_seq:06d}", client, product, amount,
                        round(amount * rng.uniform(0.25, 1.0), 0), round(rate + rng.uniform(-3, 3), 2), 0)
        self.credits.append(credit)
        return credit

    def _volume(self, base: float, day: date) -> int:
        return max(1, int(base * WEEKDAY_FACTOR[day.weekday()] * self.rng.gauss(1, 0.06)))

    @staticmethod
    def _grade(days: int) -> str:
        return "A" if days <= 30 else "B" if days <= 60 else "C" if days <= 90 else "D" if days <= 180 else "E"

    # ------------------------------------------------------------- daily loads
    def generate_day(self, day: date, scenarios: dict[str, str]) -> dict[str, Load]:
        """Produce every table's load for `day`. `scenarios` maps table -> scenario id."""
        rng = self.rng
        iso = day.isoformat()
        loads: dict[str, Load] = {}

        # clientes ------------------------------------------------------------
        rows = []
        for _ in range(self._volume(45, day)):
            self._doc_seq += 1
            doc_type = rng.choices(("CC", "CE", "NIT"), (90, 4, 6))[0]
            rows.append({
                "id_cliente": self._new_client(), "tipo_documento": doc_type,
                "numero_documento": str(1_000_000_000 + self._doc_seq * 7919 % 8_999_999_999),
                "nombre_cliente": f"{rng.choice(FIRST)} {rng.choice(LAST)} {rng.choice(LAST)}",
                "segmento": rng.choices(("personas", "pyme", "empresarial"), (85, 12, 3))[0],
                "ingresos_mensuales": round(rng.lognormvariate(math.log(3_500_000), 0.6), -3),
                "fecha_vinculacion": iso,
            })
        loads["clientes"] = Load("clientes", rows, BY_NAME["clientes"].column_names, None)

        # desembolsos (create new credits) ---------------------------------------
        rows = []
        for i in range(self._volume(90, day)):
            credit = self._new_credit(rng.choice(self.clients[-400:]))
            credit.balance = credit.amount
            rows.append({
                "id_desembolso": f"DS-{day:%Y%m%d}-{i:04d}", "id_credito": credit.id, "id_cliente": credit.client,
                "fecha_desembolso": iso, "producto": credit.product, "valor_desembolso": credit.amount,
                "oficina": rng.choice(OFFICES),
            })
        loads["desembolsos"] = Load("desembolsos", rows, BY_NAME["desembolsos"].column_names, None)

        # pagos + portfolio evolution -------------------------------------------
        rows = []
        current = [c for c in self.credits if c.days_late == 0]
        late = [c for c in self.credits if c.days_late > 0]
        payers = rng.sample(current, min(len(current), self._volume(650, day)))
        payers += [c for c in late if rng.random() < 0.02]  # a few delinquent clients catch up
        for i, credit in enumerate(payers):
            value = max(20_000.0, round(credit.balance * rng.uniform(0.01, 0.05), -2))
            credit.balance = max(credit.amount * 0.05, credit.balance - value)
            credit.days_late = 0
            rows.append({
                "id_pago": f"PG-{day:%Y%m%d}-{i:05d}", "id_credito": credit.id, "fecha_pago": iso,
                "valor_pago": value, "canal": rng.choices(("app", "pse", "oficina", "corresponsal"), (45, 25, 15, 15))[0],
                "estado": "aplicado" if rng.random() > 0.03 else "reversado",
            })
        loads["pagos"] = Load("pagos", rows, BY_NAME["pagos"].column_names, None)

        paid = {c.id for c in payers}
        for credit in self.credits:
            if credit.id in paid:
                continue
            if credit.days_late > 0:
                credit.days_late += 1
            elif rng.random() < 0.002:
                credit.days_late = 1
        # Keep the portfolio stable: about as many credits are paid off / written off as are disbursed.
        new_ids = {r["id_credito"] for r in loads["desembolsos"].rows}
        closable = [c for c in self.credits if c.days_late == 0 and c.id not in new_ids]
        closed = {c.id for c in rng.sample(closable, min(len(closable), len(new_ids)))}
        self.credits = [c for c in self.credits if c.id not in closed and c.days_late < 360]

        # cartera snapshot ----------------------------------------------------------
        rows = [{
            "id_credito": c.id, "id_cliente": c.client, "producto": c.product, "fecha_corte": iso,
            "monto_desembolsado": c.amount, "saldo_capital": round(c.balance, 0), "tasa_interes_ea": c.rate,
            "dias_mora": c.days_late, "calificacion": self._grade(c.days_late),
        } for c in self.credits]
        loads["cartera_creditos"] = Load("cartera_creditos", rows, BY_NAME["cartera_creditos"].column_names, None)

        # indicadores (computed from the snapshot) ------------------------------
        agg: dict[str, list[float]] = {p: [0, 0.0, 0.0] for p in PRODUCTS}
        for c in self.credits:
            a = agg[c.product]
            a[0] += 1
            a[1] += c.balance
            if c.days_late > 30:
                a[2] += c.balance
        rows = [{
            "fecha_corte": iso, "producto": p, "numero_creditos": int(n), "saldo_total": round(total, 0),
            "saldo_vencido": round(late, 0), "tasa_mora": round(100 * late / total, 2) if total else 0.0,
        } for p, (n, total, late) in agg.items()]
        loads["indicadores_cartera"] = Load("indicadores_cartera", rows,
                                            BY_NAME["indicadores_cartera"].column_names, None)

        # tasas -----------------------------------------------------------------------
        self._trm *= math.exp(rng.gauss(0, 0.004))
        self._ibr = max(4.0, self._ibr + rng.gauss(0, 0.01))
        self._dtf = max(4.0, self._dtf + rng.gauss(0, 0.01))
        rows = [{"fecha": iso, "indicador": "TRM", "valor": round(self._trm, 2)},
                {"fecha": iso, "indicador": "IBR_1M", "valor": round(self._ibr, 3)},
                {"fecha": iso, "indicador": "DTF", "valor": round(self._dtf, 3)}]
        loads["tasas_mercado"] = Load("tasas_mercado", rows, BY_NAME["tasas_mercado"].column_names, None)

        # arrival times + bad loads -------------------------------------------------
        for name, load in loads.items():
            load.arrives_at = BY_NAME[name].expected_at + rng.randint(-15, 25)
            if name in scenarios:
                self._apply(scenarios[name], load, day)
        self._yesterday = day
        return loads

    def _apply(self, scenario: str, load: Load, day: date) -> None:
        rng = self.rng
        rows = load.rows
        if scenario == "no_llega":
            load.arrives_at = None
        elif scenario == "llega_tarde":
            load.arrives_at = BY_NAME[load.table].expected_at + 120
        elif scenario == "ingesta_borrada":
            load.rows = []
        elif scenario == "carga_parcial":
            load.rows = rows[: int(len(rows) * 0.35)]
        elif scenario == "carga_duplicada":
            load.rows = rows + [dict(r) for r in rows]
        elif scenario == "archivo_de_ayer":
            yesterday = (day - timedelta(days=1)).isoformat()
            for r in rows:
                r["fecha_corte"] = yesterday
        elif scenario == "saldo_mayor_monto":
            for r in rng.sample(rows, max(1, len(rows) // 12)):
                r["saldo_capital"] = round(r["monto_desembolsado"] * rng.uniform(1.1, 1.6), 0)
        elif scenario == "tasa_mora_negativa":
            target = rng.choice(rows)
            target["tasa_mora"] = -abs(target["tasa_mora"]) - 1.3
        elif scenario == "pico_desembolsos":
            for r in rows:
                if rng.random() < 0.6:
                    r["valor_desembolso"] = r["valor_desembolso"] * 10
        elif scenario == "nulos_documento":
            for r in rows:
                if rng.random() < 0.3:
                    r["numero_documento"] = None
        elif scenario == "canal_invalido":
            for r in rng.sample(rows, max(1, len(rows) // 8)):
                r["canal"] = "DESCONOCIDO"
        elif scenario == "columna_eliminada":
            for r in rows:
                r.pop("canal", None)
            load.columns = [c for c in load.columns if c != "canal"]
        elif scenario == "trm_atipica":
            rows[0]["valor"] = round(rows[0]["valor"] * 10, 2)
