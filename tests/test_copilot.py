from conftest import open_incidents

from atlas import copilot


def test_rules_copilot_explains_with_evidence(warm):
    warm.inject("schema_drift")
    warm.tick()
    inc = open_incidents(warm)[0]
    result = copilot.analyze(warm, inc.id, use_llm=False)
    assert result["mode"] == "rules"
    assert "amount" in result["probable_root_cause"]
    assert any("Quarantine reason" in e for e in result["evidence"])
    assert "reg.regulatory_report" in result["affected_assets"]
    assert result["remediation_steps"]
    assert inc.severity in result["stakeholder_update"]


def test_llm_failure_falls_back_to_rules(warm, monkeypatch):
    warm.inject("duplicate_load")
    warm.tick()
    inc = open_incidents(warm)[0]

    def boom(*_args, **_kwargs):
        raise RuntimeError("no network")

    monkeypatch.setattr(copilot, "claude_analysis", boom)
    result = copilot.analyze(warm, inc.id, use_llm=True)
    assert result["mode"] == "rules"
    assert "no network" in result["fallback_reason"]
