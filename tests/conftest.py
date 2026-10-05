import os

os.environ.setdefault("ATLAS_AUTOSTART", "0")  # la API no arranca su reloj en los tests
os.environ.setdefault("ATLAS_RULES_PATH", "")  # reglas en memoria
os.environ.setdefault("ATLAS_RANDOM_ANOMALIES", "0")

from datetime import date  # noqa: E402

import pytest  # noqa: E402

from atlas.config import Settings  # noqa: E402
from atlas.engine import Engine  # noqa: E402

START = date(2026, 10, 5)  # un lunes


def make_engine(**kw) -> Engine:
    return Engine(Settings(seed=7, rules_path=None, random_anomalies=False, start_date=START, **kw))


@pytest.fixture
def engine():
    """Motor con 70 días de historia, parado a las 05:30 del día de hoy (ninguna tabla ha llegado)."""
    e = make_engine()
    yield e
    e.close()


def open_incidents(engine: Engine):
    return [i for i in engine.incidents.values() if i.resolved is None]


def next_day(engine: Engine) -> None:
    engine.run_day()
    engine.tick()
