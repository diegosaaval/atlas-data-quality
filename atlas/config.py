"""Runtime configuration, read once from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    return default if raw is None else raw.strip().lower() in {"1", "true", "yes", "on", "si", "sí"}


@dataclass(frozen=True)
class Settings:
    seed: int = field(default_factory=lambda: int(os.getenv("ATLAS_SEED", "7")))
    tick_seconds: float = field(default_factory=lambda: float(os.getenv("ATLAS_TICK_SECONDS", "1.0")))
    autostart: bool = field(default_factory=lambda: _env_bool("ATLAS_AUTOSTART", True))
    db_path: str = field(default_factory=lambda: os.getenv("ATLAS_DB_PATH", ":memory:"))
    rules_path: str | None = field(default_factory=lambda: os.getenv("ATLAS_RULES_PATH", "data/reglas.json"))
    random_anomalies: bool = field(default_factory=lambda: _env_bool("ATLAS_RANDOM_ANOMALIES", True))
    # Demo pública (internet): protege las reglas base y evita que un visitante deje todo en pausa o lo reinicie.
    public_demo: bool = field(default_factory=lambda: _env_bool("ATLAS_PUBLIC_DEMO", False))
    # Fuente de datos: vacío = banco simulado (demo). Un nombre = conectores/<nombre>.yaml (p. ej. "finflow").
    source: str = field(default_factory=lambda: os.getenv("ATLAS_FUENTE", "").strip())
    source_config: str | None = field(default_factory=lambda: os.getenv("ATLAS_FUENTE_CONFIG") or None)
    source_path: str | None = field(default_factory=lambda: os.getenv("ATLAS_FUENTE_RUTA") or None)
    max_user_rules: int = 15
    max_writes_per_minute: int = 40  # por visitante, solo en la demo pública
    max_ws_clients: int = 300
    max_pause_seconds: int = 120
    start_date: date = field(default_factory=date.today)
    copilot_model: str = field(default_factory=lambda: os.getenv("ATLAS_COPILOT_MODEL", "claude-opus-5-5"))
    github_url: str = field(default_factory=lambda: os.getenv("ATLAS_GITHUB_URL", "https://github.com/diegosaaval/atlas-data-quality"))


def get_settings() -> Settings:
    return Settings()
