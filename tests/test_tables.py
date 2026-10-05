from datetime import date

from atlas.tables import BY_NAME, SCENARIOS, TABLES, SyntheticBank

DAY = date(2026, 10, 5)


def test_generation_is_deterministic():
    a, b = SyntheticBank(3), SyntheticBank(3)
    la, lb = a.generate_day(DAY, {}), b.generate_day(DAY, {})
    assert {k: v.rows for k, v in la.items()} == {k: v.rows for k, v in lb.items()}


def test_every_table_is_generated_with_its_columns():
    loads = SyntheticBank(1).generate_day(DAY, {})
    assert set(loads) == {t.name for t in TABLES}
    for name, load in loads.items():
        assert load.rows, name
        assert set(load.rows[0]) == set(BY_NAME[name].column_names)


def test_portfolio_is_stable_and_delinquency_is_realistic():
    bank = SyntheticBank(5)
    for i in range(90):
        loads = bank.generate_day(date.fromordinal(DAY.toordinal() + i), {})
    assert 1600 <= len(loads["cartera_creditos"].rows) <= 2000
    rates = [r["tasa_mora"] for r in loads["indicadores_cartera"].rows]
    assert all(0 <= r < 15 for r in rates)
    assert 1 < sum(rates) / len(rates) < 10


def test_weekends_are_quieter():
    bank = SyntheticBank(2)
    monday = len(bank.generate_day(date(2026, 10, 5), {})["pagos"].rows)
    for d in range(6, 11):
        bank.generate_day(date(2026, 10, d), {})
    sunday = len(bank.generate_day(date(2026, 10, 11), {})["pagos"].rows)
    assert sunday < monday * 0.5


def test_every_scenario_changes_its_table():
    for sc in SCENARIOS:
        clean = SyntheticBank(9).generate_day(DAY, {})[sc.table]
        bad = SyntheticBank(9).generate_day(DAY, {sc.table: sc.id})[sc.table]
        assert (bad.rows, bad.columns, bad.arrives_at) != (clean.rows, clean.columns, clean.arrives_at), sc.id
