import os

os.environ.setdefault("ATLAS_AUTOSTART", "0")  # the API must not start its background loop in tests

import pytest  # noqa: E402

from atlas.config import Settings  # noqa: E402
from atlas.engine import Engine  # noqa: E402


@pytest.fixture
def engine() -> Engine:
    return Engine(Settings(seed=7, db_path=":memory:", autostart=False))


@pytest.fixture
def warm(engine: Engine) -> Engine:
    """Engine with ~3 simulated hours of history so baselines are learned."""
    for _ in range(40):
        engine.tick()
    return engine


def run_until(engine: Engine, predicate, max_ticks: int = 15) -> bool:
    for _ in range(max_ticks):
        engine.tick()
        if predicate(engine):
            return True
    return False


def open_incidents(engine: Engine):
    return [i for i in engine.incidents.values() if i.status != "resolved"]
