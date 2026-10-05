"""Conectores: ATLAS monitoreando cualquier tabla real en vez del banco simulado.

Un conector se describe en un YAML (ver conectores/): dónde están los archivos (Parquet o CSV),
qué tablas monitorear, su responsable y, opcionalmente, sus reglas. ATLAS lee la estructura de
cada tabla por su cuenta y, si no le das reglas, propone unas iniciales a partir del perfil de
los datos. Todo se lee con DuckDB en modo solo lectura.

Cuándo hay "carga nueva":
* con manifiesto (como FINFLOW): cada vez que cambia `published_at`, por cada fecha procesada;
* sin manifiesto: cada vez que cambia la fecha de modificación de los archivos.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml

from .tables import Column, TableSpec

ROOT = Path(__file__).resolve().parent.parent
TYPE_MAP = {"VARCHAR": "texto", "BIGINT": "entero", "INTEGER": "entero", "SMALLINT": "entero", "TINYINT": "entero",
            "HUGEINT": "entero", "BOOLEAN": "entero", "DOUBLE": "decimal", "FLOAT": "decimal", "REAL": "decimal",
            "DATE": "fecha", "TIMESTAMP": "fecha", "TIMESTAMP WITH TIME ZONE": "fecha"}


def _type(duck_type: str) -> str:
    base = duck_type.split("(")[0].upper()
    return "decimal" if base == "DECIMAL" else TYPE_MAP.get(base, "texto")


def _plain(value: Any) -> Any:
    """Valores de cualquier tabla a tipos que SQLite guarda tal cual."""
    if value is None or isinstance(value, (str, int, float)) and not isinstance(value, bool):
        return value
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime):
        return value.replace(tzinfo=None).isoformat(sep=" ")
    if isinstance(value, (date, time)):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.hex()
    return str(value)  # UUID, intervalos, listas, estructuras…


@dataclass
class ParquetSource:
    """Tablas en Parquet publicadas por otro sistema, con un manifiesto por publicación."""

    name: str
    title: str
    description: str
    path: Path
    manifest_file: str
    expected_at: int
    owner: str
    owner_email: str
    project_url: str
    tables: list[TableSpec]
    load_types: dict[str, str]
    date_columns: dict[str, str]
    rules: list[dict[str, Any]] = field(default_factory=list)
    files: dict[str, Path] = field(default_factory=dict)
    auto_rules: set[str] = field(default_factory=set)
    run_url: str = ""  # plantilla con {run_id}: enlace a la corrida en la herramienta de la fuente

    # ------------------------------------------------------------------ lectura
    def _file(self, table: str) -> Path:
        return self.files[table]

    def read_manifest(self) -> dict[str, Any] | None:
        if not self.manifest_file:
            return None
        path = self.path / self.manifest_file
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if data.get("published_at") else None

    def fingerprint(self) -> str:
        """Cambia cada vez que hay algo nuevo que validar (manifiesto o archivos modificados)."""
        manifest = self.read_manifest()
        if manifest:
            return str(manifest["published_at"])
        stamps = [f.stat().st_mtime for f in self.files.values() if f.exists()]
        return f"mtime:{max(stamps):.0f}" if stamps else ""

    def available_dates(self) -> set[date]:
        """Fechas presentes en las tablas incrementales (la historia completa que ya está publicada)."""
        import duckdb

        out: set[date] = set()
        con = duckdb.connect(":memory:")
        try:
            for spec in self.tables:
                col, file = self.date_columns.get(spec.name), self.files.get(spec.name)
                if self.load_types.get(spec.name) != "incremental" or not col or not file or not file.exists():
                    continue
                rel = _relation(con, file)
                if col in rel.columns:
                    out |= {d for (d,) in rel.project(f'CAST("{col}" AS DATE)').distinct().fetchall() if d}
        finally:
            con.close()
        return out

    def read(self, spec: TableSpec, day: date | None) -> tuple[list[dict[str, Any]], list[str]]:
        """Filas de una tabla: la partición de `day` (incremental) o la foto completa (snapshot)."""
        import duckdb

        file = self._file(spec.name)
        if not file.exists():
            return [], []
        con = duckdb.connect(":memory:", read_only=False)  # conexión efímera; el Parquet solo se lee
        try:
            rel = _relation(con, file)
            columns = list(rel.columns)
            date_col = self.date_columns.get(spec.name)
            if day is not None and self.load_types.get(spec.name) == "incremental" and date_col in columns:
                rel = rel.filter(f'CAST("{date_col}" AS DATE) = DATE \'{day.isoformat()}\'')
            rows = [dict(zip(columns, (_plain(v) for v in row), strict=True)) for row in rel.fetchall()]
            return rows, columns
        finally:
            con.close()


def _relation(con, file: Path):
    if file.suffix.lower() == ".csv":
        return con.read_csv(str(file), header=True)
    return con.read_parquet(str(file))


def _columns(file: Path) -> list[Column]:
    """Lee la estructura base de la tabla: nombre y tipo de cada columna."""
    import duckdb

    con = duckdb.connect(":memory:")
    try:
        rel = _relation(con, file)
        described = list(zip(rel.columns, (str(t) for t in rel.types), strict=True))
    finally:
        con.close()
    return [Column(name, _type(kind), "") for name, kind in described]


def suggest_rules(spec: TableSpec, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Reglas iniciales a partir del perfil de los datos (para tablas sin reglas definidas).

    Son un punto de partida conservador: identificadores sin vacíos ni duplicados, números
    no negativos si nunca lo fueron, y catálogos cortos como valores permitidos.
    """
    if not rows:
        return []
    out: list[dict[str, Any]] = []
    n = len(rows)
    for col in spec.columns:
        values = [r.get(col.name) for r in rows]
        present = [v for v in values if v not in (None, "")]
        nulls = n - len(present)
        lname = col.name.lower()
        is_id = lname.startswith("id_") or lname.endswith("_id") or lname == "id"
        if is_id and nulls == 0:
            out.append({"table": spec.name, "type": "no_nulos", "severity": "alta", "params": {"column": col.name},
                        "note": "Sugerida por ATLAS: el identificador nunca vino vacío."})
            if len(set(present)) == n and spec.load_type == "incremental":
                out.append({"table": spec.name, "type": "unico", "severity": "critica",
                            "params": {"columns": [col.name]}, "note": "Sugerida por ATLAS: el identificador es único."})
        if col.type in ("entero", "decimal") and present and not is_id and min(present) >= 0:
                out.append({"table": spec.name, "type": "rango", "severity": "media",
                            "params": {"column": col.name, "min": 0}, "note": "Sugerida por ATLAS: nunca fue negativo."})
        if col.type == "texto" and present and not is_id:
            distinct = sorted({str(v) for v in present})
            if 1 < len(distinct) <= 12 and len(present) >= 30:
                out.append({"table": spec.name, "type": "valores_permitidos", "severity": "media",
                            "params": {"column": col.name, "values": distinct},
                            "note": "Sugerida por ATLAS: catálogo observado en los datos."})
    return out


def load_source(name: str, config: str | None = None, path_override: str | None = None) -> ParquetSource:
    """Carga un conector desde conectores/<name>.yaml (o la ruta `config`)."""
    cfg_path = Path(config) if config else ROOT / "conectores" / f"{name}.yaml"
    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    raw_path = path_override or os.getenv("ATLAS_FUENTE_RUTA") or os.getenv(f"ATLAS_{name.upper()}_GOLD") or cfg["ruta"]
    base = Path(raw_path).expanduser()
    if not base.is_absolute():
        base = (ROOT / base).resolve()
    h, m = (int(x) for x in str(cfg.get("hora_esperada", "07:00")).split(":"))
    fmt = cfg.get("formato", "parquet")
    specs, load_types, date_columns, files = [], {}, {}, {}
    for t in cfg["tablas"]:
        file = base / t.get("archivo", f"{t['nombre']}.{fmt}")
        if not file.exists():
            raise FileNotFoundError(f"No encuentro {file}. Revisa la ruta del conector (o ATLAS_FUENTE_RUTA).")
        specs.append(TableSpec(
            t["nombre"], t.get("titulo", t["nombre"]), t.get("descripcion", ""), t.get("responsable", cfg.get("responsable", name)),
            t.get("correo", cfg.get("correo", "")), h * 60 + m, t.get("tipo", "incremental"), t.get("columna_fecha", ""),
            tuple(_columns(file)), tuple(t.get("consumidores", [])),
        ))
        load_types[t["nombre"]] = t.get("tipo", "incremental")
        date_columns[t["nombre"]] = t.get("columna_fecha", "")
        files[t["nombre"]] = file
    rules = cfg.get("reglas") or []
    with_rules = {r["table"] for r in rules}
    auto = {s.name for s in specs if s.name not in with_rules} if cfg.get("reglas_automaticas", True) else set()
    return ParquetSource(cfg.get("fuente", name), cfg.get("titulo", name), cfg.get("descripcion", ""), base,
                         cfg.get("manifiesto", ""), h * 60 + m, cfg.get("responsable", name),
                         cfg.get("correo", ""), cfg.get("url_proyecto", ""), specs, load_types, date_columns,
                         rules, files, auto, cfg.get("url_corrida", ""))


def list_sources() -> list[dict[str, Any]]:
    """Conectores configurados en conectores/*.yaml y si sus datos están disponibles en este equipo."""
    out = []
    for cfg in sorted((ROOT / "conectores").glob("*.yaml")):
        name = cfg.stem
        try:
            meta = yaml.safe_load(cfg.read_text(encoding="utf-8"))
            src = load_source(name)
            out.append({"name": name, "title": meta.get("titulo", name), "description": meta.get("descripcion", ""),
                        "tables": [t.name for t in src.tables], "path": str(src.path), "available": True})
        except Exception as exc:  # una fuente mal configurada no debe tumbar la lista
            out.append({"name": name, "title": name, "description": "", "tables": [], "path": "",
                        "available": False, "error": str(exc)})
    return out
