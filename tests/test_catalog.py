from atlas import catalog


def test_every_upstream_exists():
    for d in catalog.DATASETS:
        for up in d.upstream:
            assert up in catalog.BY_ID, f"{d.id} references unknown upstream {up}"


def test_graph_is_acyclic():
    for d in catalog.DATASETS:
        assert d.id not in catalog.ancestors(d.id)


def test_layers_flow_forward():
    order = {layer: i for i, layer in enumerate(catalog.LAYERS)}
    for up, down in catalog.edges():
        assert order[catalog.get(up).layer] <= order[catalog.get(down).layer]


def test_blast_radius_reaches_regulatory_report_and_is_sorted_by_criticality():
    radius = catalog.blast_radius("bronze.transactions")
    ids = [d.id for d in radius]
    assert "reg.regulatory_report" in ids
    assert "bronze.accounts" not in ids
    weights = [d.weight for d in radius]
    assert weights == sorted(weights, reverse=True)


def test_consumers_have_no_children():
    for d in catalog.DATASETS:
        if d.layer == "consumer":
            assert catalog.children(d.id) == ()
