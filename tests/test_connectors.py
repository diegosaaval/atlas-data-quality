import json
import os
from datetime import date, datetime, timedelta

import duckdb
import pytest

from atlas import tables
from atlas.config import Settings
from atlas.engine import Engine

START = date(2026, 9, 1)


def write_gold(folder, days=10, dup_on=None, published="2026-10-05T12:00:00+00:00"):
    """Una mini 'FINFLOW': pagos (incremental), indicadores (incremental) y clientes (snapshot)."""
    folder.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    con.execute("""CREATE TABLE pagos AS
        SELECT 'P-' || d || '-' || i AS id_pago, 'C-' || (i % 40) AS id_cliente,
               CASE WHEN i % 3 = 0 THEN 'pse' ELSE 'card' END AS medio_pago,
               1000.0 + i AS monto, DATE '2026-09-01' + CAST(d AS INTEGER) AS fecha
        FROM range(?) t(d), range(60) s(i)""", [days])
    if dup_on is not None:
        con.execute("INSERT INTO pagos SELECT * FROM pagos WHERE fecha = DATE '2026-09-01' + CAST(? AS INTEGER)", [dup_on])
    con.execute("""CREATE TABLE indicadores AS SELECT DATE '2026-09-01' + CAST(d AS INTEGER) AS fecha, 0.9 AS tasa_aprobacion,
                   60 AS pagos FROM range(?) t(d)""", [days])
    con.execute("CREATE TABLE clientes AS SELECT 'C-' || i AS id_cliente, 'CC' AS tipo_documento FROM range(40) t(i)")
    for t, f in (("pagos", "pagos_gold"), ("indicadores", "indicadores_financieros"), ("clientes", "clientes_gold")):
        con.execute(f"COPY {t} TO '{folder / f}.parquet' (FORMAT parquet)")
    con.close()
    dates = [(START + timedelta(days=d)).isoformat() for d in range(days)]
    (folder / "_manifest.json").write_text(json.dumps({"run_id": f"run-{days}", "dates": dates,
                                                       "published_at": published, "datasets": {}}))


def write_config(path, gold, expected="23:59"):
    path.write_text(f"""
fuente: prueba
titulo: Fuente de prueba
ruta: {gold}
manifiesto: _manifest.json
hora_esperada: "{expected}"   # por defecto 23:59: el test no depende de la zona horaria
tablas:
  - {{nombre: pagos_gold, tipo: incremental, columna_fecha: fecha}}
  - {{nombre: indicadores_financieros, tipo: incremental, columna_fecha: fecha}}
  - {{nombre: clientes_gold, tipo: snapshot}}
reglas:
  - {{table: pagos_gold, type: unico, severity: critica, params: {{columns: [id_pago]}}}}
""")


@pytest.fixture
def source(tmp_path):
    gold, cfg = tmp_path / "gold", tmp_path / "prueba.yaml"
    write_gold(gold)
    write_config(cfg, gold)
    engine = Engine(Settings(source="prueba", source_config=str(cfg), rules_path=None, random_anomalies=False))
    yield engine, gold
    engine.close()
    tables.use_tables(tables.BANK_TABLES)  # no contaminar los demás tests


def test_reads_the_structure_of_any_table_and_validates_the_history(source):
    engine, _ = source
    snap = engine.snapshot()
    assert snap["mode"] == "conector" and snap["scenarios"] == []
    assert [t["name"] for t in snap["tables"]] == ["pagos_gold", "indicadores_financieros", "clientes_gold"]
    cols = {c.name: c.type for c in tables.BY_NAME["pagos_gold"].columns}
    assert cols == {"id_pago": "texto", "id_cliente": "texto", "medio_pago": "texto", "monto": "decimal", "fecha": "fecha"}
    assert snap["source"]["dates"] == 10 and snap["source"]["last_date"] == "2026-09-10"
    assert snap["kpis"]["open_incidents"] == 0
    assert all(t["status"] == "ok" for t in snap["tables"])
    # una foto completa (snapshot) se valida solo en la fecha más reciente: sin días "no llegó" inventados
    assert engine.store.scalar("SELECT COUNT(*) FROM metrics WHERE tabla = 'clientes_gold' AND metrica = 'score'") == 1
    assert engine.store.scalar("SELECT COUNT(*) FROM check_results WHERE tabla = 'clientes_gold' AND estado = 'falla'") == 0


def test_tables_without_rules_get_suggested_rules(source):
    engine, _ = source
    suggested = [r for r in engine.rules.rules.values() if r.author == "sugerida"]
    kinds = {(r.table, r.type) for r in suggested}
    assert ("clientes_gold", "no_nulos") in kinds
    assert ("indicadores_financieros", "rango") in kinds
    assert not any(r.table == "pagos_gold" for r in suggested)  # tiene reglas propias


def test_new_publication_with_duplicates_opens_an_incident(source):
    engine, gold = source
    write_gold(gold, days=11, dup_on=10, published="2026-10-06T12:00:00+00:00")
    engine.tick()
    inc = [i for i in engine.incidents.values() if i.table == "pagos_gold" and i.resolved is None]
    assert inc and any(c["rule_type"] == "unico" for c in inc[0].checks)
    assert engine.snapshot()["source"]["dates"] == 11


def test_republishing_a_day_replaces_it_instead_of_duplicating(source):
    engine, gold = source
    write_gold(gold, days=10, published="2026-10-05T15:00:00+00:00")  # misma fecha, nueva corrida
    engine.tick()
    n = engine.store.scalar("SELECT COUNT(*) FROM t_pagos_gold WHERE _fecha_carga = '2026-09-10'")
    assert n == 60
    checks = engine.store.scalar("SELECT COUNT(*) FROM check_results WHERE tabla = 'pagos_gold' AND fecha = '2026-09-10'")
    assert checks == len(engine.tables["pagos_gold"].results)


def test_backfilled_dates_are_not_flagged_late_but_daily_publications_are(tmp_path):
    gold, cfg = tmp_path / "gold", tmp_path / "prueba.yaml"
    write_gold(gold)  # septiembre publicado el 5 de octubre: un backfill
    write_config(cfg, gold, expected="00:30")
    engine = Engine(Settings(source="prueba", source_config=str(cfg), rules_path=None, random_anomalies=False))
    try:
        assert all(t["status"] == "ok" for t in engine.snapshot()["tables"])
        # la publicación diaria del 11 de septiembre llega al día siguiente a las 12:00 (hora local): tarde
        daily = datetime(2026, 9, 12, 12, 0).astimezone().isoformat()
        write_gold(gold, days=11, published=daily)
        engine._sync()  # sin tick: el reloj real (octubre) abriría además el aviso de "no ha publicado hoy"
        availability = engine.tables["pagos_gold"].results[0]
        assert availability.check_id == "disponibilidad" and availability.status == "advertencia"
    finally:
        engine.close()
        tables.use_tables(tables.BANK_TABLES)


def test_csv_source_without_manifest_validates_when_files_change(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    (data / "ventas.csv").write_text("id_venta,monto,region\n" + "\n".join(f"V{i},{100 + i},{'norte' if i % 2 else 'sur'}"
                                                                          for i in range(50)))
    cfg = tmp_path / "ventas.yaml"
    cfg.write_text(f"fuente: ventas\nruta: {data}\nformato: csv\ntablas:\n  - {{nombre: ventas, tipo: snapshot}}\n")
    engine = Engine(Settings(source="ventas", source_config=str(cfg), rules_path=None, random_anomalies=False))
    try:
        assert engine.snapshot()["tables"][0]["rows"] == 50
        types = {r.type for r in engine.rules.rules.values()}
        assert {"no_nulos", "rango", "valores_permitidos"} <= types
        (data / "ventas.csv").write_text("id_venta,monto,region\nV1,-5,oeste\n")
        os.utime(data / "ventas.csv", (1e10, 1e10))
        engine.tick()
        assert engine.tables["ventas"].status == "falla"  # monto negativo y región desconocida
    finally:
        engine.close()
        tables.use_tables(tables.BANK_TABLES)


def test_missing_source_files_give_a_clear_error(tmp_path):
    cfg = tmp_path / "x.yaml"
    cfg.write_text(f"fuente: x\nruta: {tmp_path}\ntablas:\n  - {{nombre: no_existe}}\n")
    with pytest.raises(FileNotFoundError, match="No encuentro"):
        Engine(Settings(source="x", source_config=str(cfg), rules_path=None))
    tables.use_tables(tables.BANK_TABLES)
