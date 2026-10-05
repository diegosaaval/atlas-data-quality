"""Runtime configuration, read once from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    seed: int = field(default_factory=lambda: int(os.getenv("ATLAS_SEED", "7")))
    tick_seconds: float = field(default_factory=lambda: float(os.getenv("ATLAS_TICK_SECONDS", "1.5")))
    tick_minutes: int = 5  # simulated minutes per pipeline run
    autostart: bool = field(default_factory=lambda: _env_bool("ATLAS_AUTOSTART", True))
    db_path: str = field(default_factory=lambda: os.getenv("ATLAS_DB_PATH", ":memory:"))
    default_role: str = field(default_factory=lambda: os.getenv("ATLAS_DEFAULT_ROLE", "engineer"))
    retention_ticks: int = 288  # 24 simulated hours
    copilot_model: str = field(default_factory=lambda: os.getenv("ATLAS_COPILOT_MODEL", "claude-opus-5-5"))
    github_url: str = field(default_factory=lambda: os.getenv("ATLAS_GITHUB_URL", "https://github.com/"))


def get_settings() -> Settings:
    return Settings()
