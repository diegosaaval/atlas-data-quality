"""Dataset catalog and lineage graph.

The catalog is the single source of truth for ownership, criticality, SLAs and
lineage. Everything else (quality gate, blast radius, incident severity) is
derived from it.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import cache

LAYERS = ("source", "bronze", "silver", "gold", "consumer")
CRITICALITY_WEIGHT = {"tier1": 3, "tier2": 2, "tier3": 1}


@dataclass(frozen=True)
class Dataset:
    id: str
    name: str
    layer: str
    domain: str
    owner: str
    criticality: str
    sla_minutes: int
    upstream: tuple[str, ...]
    description: str

    @property
    def weight(self) -> int:
        return CRITICALITY_WEIGHT[self.criticality]


DATASETS: tuple[Dataset, ...] = (
    # --- Sources (systems we do not own) -------------------------------------
    Dataset("src.core_banking.accounts", "Core banking · accounts", "source", "core", "core-banking",
            "tier1", 30, (), "Customer accounts from the core banking system (CDC feed)."),
    Dataset("src.payments.transactions", "Payments gateway · transactions", "source", "payments", "payments-platform",
            "tier1", 15, (), "Card, PSE and transfer events from the payments gateway."),
    Dataset("src.payments.replica", "Payments · read replica", "source", "payments", "payments-platform",
            "tier2", 15, (), "Read replica used to reconcile counts and amounts with the primary."),
    Dataset("src.fx.rates", "FX provider · rates", "source", "treasury", "treasury",
            "tier2", 15, (), "USD/COP reference rates from the market data provider."),
    # --- Bronze (raw, append-only) -------------------------------------------
    Dataset("bronze.accounts", "bronze.accounts", "bronze", "core", "data-platform",
            "tier1", 30, ("src.core_banking.accounts",), "Raw account payloads, one row per CDC event."),
    Dataset("bronze.transactions", "bronze.transactions", "bronze", "payments", "data-platform",
            "tier1", 15, ("src.payments.transactions", "src.payments.replica"),
            "Raw transaction payloads, partitioned by 5-minute batch."),
    Dataset("bronze.fx_rates", "bronze.fx_rates", "bronze", "treasury", "data-platform",
            "tier2", 15, ("src.fx.rates",), "Raw FX quotes."),
    # --- Silver (validated, conformed) ---------------------------------------
    Dataset("silver.accounts", "silver.accounts", "silver", "core", "data-platform",
            "tier1", 30, ("bronze.accounts",), "Contract-validated accounts, deduplicated by account_id."),
    Dataset("silver.transactions", "silver.transactions", "silver", "payments", "data-platform",
            "tier1", 20, ("bronze.transactions", "silver.accounts", "bronze.fx_rates"),
            "Validated transactions, deduplicated, enriched with COP amounts."),
    # --- Gold (data products) ------------------------------------------------
    Dataset("gold.daily_ledger", "gold.daily_ledger", "gold", "finance", "finance-analytics",
            "tier1", 30, ("silver.transactions",), "Daily ledger by category and currency."),
    Dataset("gold.risk_exposure", "gold.risk_exposure", "gold", "risk", "risk-analytics",
            "tier1", 30, ("silver.transactions", "silver.accounts"), "Exposure by customer segment and risk tier."),
    Dataset("gold.fraud_features", "gold.fraud_features", "gold", "fraud", "fraud-ml",
            "tier2", 30, ("silver.transactions",), "Per-account behavioural features (1h window)."),
    Dataset("gold.payment_ops", "gold.payment_ops", "gold", "operations", "payments-ops",
            "tier3", 30, ("silver.transactions",), "Approval, decline and reversal rates per window."),
    # --- Consumers -----------------------------------------------------------
    Dataset("bi.finance_pnl", "Finance P&L dashboard", "consumer", "finance", "finance",
            "tier2", 60, ("gold.daily_ledger",), "Daily P&L consumed by the CFO office."),
    Dataset("reg.regulatory_report", "Regulatory report", "consumer", "risk", "compliance",
            "tier1", 60, ("gold.daily_ledger", "gold.risk_exposure"), "Report submitted to the financial regulator."),
    Dataset("ml.fraud_scoring", "Fraud scoring model", "consumer", "fraud", "fraud-ml",
            "tier1", 60, ("gold.fraud_features",), "Real-time fraud model reading the feature table."),
    Dataset("ops.payments_dashboard", "Payments ops dashboard", "consumer", "operations", "payments-ops",
            "tier3", 60, ("gold.payment_ops",), "NOC dashboard for payment approval health."),
)

BY_ID: dict[str, Dataset] = {d.id: d for d in DATASETS}


def get(dataset_id: str) -> Dataset:
    return BY_ID[dataset_id]


@cache
def children(dataset_id: str) -> tuple[str, ...]:
    return tuple(d.id for d in DATASETS if dataset_id in d.upstream)


@cache
def ancestors(dataset_id: str) -> frozenset[str]:
    seen: set[str] = set()
    stack = list(BY_ID[dataset_id].upstream)
    while stack:
        node = stack.pop()
        if node not in seen:
            seen.add(node)
            stack.extend(BY_ID[node].upstream)
    return frozenset(seen)


@cache
def descendants(dataset_id: str) -> frozenset[str]:
    seen: set[str] = set()
    stack = list(children(dataset_id))
    while stack:
        node = stack.pop()
        if node not in seen:
            seen.add(node)
            stack.extend(children(node))
    return frozenset(seen)


def blast_radius(dataset_id: str) -> list[Dataset]:
    """Everything downstream of a dataset, most critical first."""
    return sorted(
        (BY_ID[d] for d in descendants(dataset_id)),
        key=lambda d: (-d.weight, LAYERS.index(d.layer), d.id),
    )


def edges() -> list[tuple[str, str]]:
    return [(up, d.id) for d in DATASETS for up in d.upstream]
