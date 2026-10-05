from datetime import date, timedelta

from atlas import monitors
from atlas.store import Store
from atlas.tables import BY_NAME

PAGOS = BY_NAME["pagos"]
DAY = date(2026, 10, 5)  # lunes


def test_availability():
    assert monitors.availability(PAGOS, 7 * 60 + 10, 7 * 60 + 15).status == "ok"
    assert monitors.availability(PAGOS, 9 * 60, 9 * 60).status == "advertencia"  # llegó, pero tarde
    late = monitors.availability(PAGOS, None, 8 * 60 + 15)
    assert late.status == "falla" and "no ha llegado" in late.message


def test_volume_learns_weekday_pattern_and_flags_drops_and_spikes():
    store = Store()
    for week in range(6, 0, -1):
        for offset, rows in ((0, 700), (6, 200)):  # lunes ~700, domingo ~200
            day = DAY - timedelta(days=7 * week) + timedelta(days=offset)
            monitors.volume(PAGOS, rows + week, store, day)
    assert monitors.volume(PAGOS, 690, store, DAY).status == "ok"
    assert monitors.volume(PAGOS, 0, store, DAY).status == "falla"
    assert monitors.volume(PAGOS, 250, store, DAY).status == "falla"   # carga parcial
    assert monitors.volume(PAGOS, 1400, store, DAY).status == "falla"  # duplicada
    assert "un lunes" in monitors.volume(PAGOS, 700, store, DAY).message
    store.close()


def test_structure():
    cols = PAGOS.column_names
    assert monitors.structure(PAGOS, cols).status == "ok"
    assert monitors.structure(PAGOS, cols + ["nueva"]).status == "advertencia"
    missing = monitors.structure(PAGOS, [c for c in cols if c != "canal"])
    assert missing.status == "falla" and "canal" in missing.message
