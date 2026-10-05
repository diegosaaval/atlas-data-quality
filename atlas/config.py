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
    start_date: date = field(default_factory=date.today)
    copilot_model: str = field(default_factory=lambda: os.getenv("ATLAS_COPILOT_MODEL", "claude-opus-5-5"))
    github_url: str = field(default_factory=lambda: os.getenv("ATLAS_GITHUB_URL", "https://github.com/"))


def get_settings() -> Settings:
    return Settings()
