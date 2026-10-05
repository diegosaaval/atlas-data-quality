"""Data contracts: declarative schemas that producers and consumers agree on.

A contract is validated at two levels:
* batch level  -> schema drift (missing / unexpected columns)
* record level -> types, required fields, enums, ranges, patterns
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from functools import cache
from pathlib import Path
from typing import Any

import yaml

CONTRACTS_DIR = Path(__file__).parent / "contracts"


@dataclass(frozen=True)
class Column:
    name: str
    type: str
    required: bool = False
    enum: tuple[Any, ...] | None = None
    min: float | None = None
    max: float | None = None
    pattern: str | None = None
    classification: str = "internal"


@dataclass(frozen=True)
class Contract:
    dataset: str
    version: str
    owner: str
    description: str
    primary_key: tuple[str, ...]
    freshness_sla_minutes: int
    columns: tuple[Column, ...]

    @property
    def column_names(self) -> set[str]:
        return {c.name for c in self.columns}

    @property
    def required_columns(self) -> list[str]:
        return [c.name for c in self.columns if c.required]

    def schema_diff(self, observed: set[str]) -> dict[str, list[str]]:
        return {
            "missing": sorted(self.column_names - observed),
            "unexpected": sorted(observed - self.column_names),
        }

    def validate(self, record: dict[str, Any]) -> list[str]:
        """Return a list of human-readable violations (empty means valid)."""
        violations: list[str] = []
        for col in self.columns:
            value = record.get(col.name)
            if value is None or value == "":
                if col.required:
                    violations.append(f"{col.name}: required")
                continue
            violation = _check_value(col, value)
            if violation:
                violations.append(f"{col.name}: {violation}")
        return violations

    def to_dict(self) -> dict[str, Any]:
        return {
            "dataset": self.dataset,
            "version": self.version,
            "owner": self.owner,
            "description": self.description,
            "primary_key": list(self.primary_key),
            "freshness_sla_minutes": self.freshness_sla_minutes,
            "columns": [
                {
                    "name": c.name, "type": c.type, "required": c.required,
                    "enum": list(c.enum) if c.enum else None, "min": c.min, "max": c.max,
                    "pattern": c.pattern, "classification": c.classification,
                }
                for c in self.columns
            ],
        }


def _check_value(col: Column, value: Any) -> str | None:
    if col.type == "string" and not isinstance(value, str):
        return f"expected string, got {type(value).__name__}"
    if col.type == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return f"expected number, got {type(value).__name__}"
        if col.min is not None and value < col.min:
            return f"{value} < min {col.min}"
        if col.max is not None and value > col.max:
            return f"{value} > max {col.max}"
    if col.type == "timestamp":
        try:
            datetime.fromisoformat(str(value))
        except ValueError:
            return "invalid timestamp"
    if col.enum is not None and value not in col.enum:
        return f"'{value}' not in enum"
    if col.pattern and isinstance(value, str) and not re.match(col.pattern, value):
        return f"does not match {col.pattern}"
    return None


def _parse(raw: dict[str, Any]) -> Contract:
    columns = tuple(
        Column(
            name=name,
            type=spec["type"],
            required=spec.get("required", False),
            enum=tuple(spec["enum"]) if "enum" in spec else None,
            min=spec.get("min"),
            max=spec.get("max"),
            pattern=spec.get("pattern"),
            classification=spec.get("classification", "internal"),
        )
        for name, spec in raw["columns"].items()
    )
    return Contract(
        dataset=raw["dataset"],
        version=str(raw["version"]),
        owner=raw["owner"],
        description=raw.get("description", ""),
        primary_key=tuple(raw.get("primary_key", [])),
        freshness_sla_minutes=int(raw.get("freshness_sla_minutes", 60)),
        columns=columns,
    )


@cache
def load_contracts() -> dict[str, Contract]:
    contracts = {}
    for path in sorted(CONTRACTS_DIR.glob("*.yaml")):
        contract = _parse(yaml.safe_load(path.read_text()))
        contracts[contract.dataset] = contract
    return contracts


def get_contract(dataset: str) -> Contract:
    return load_contracts()[dataset]
