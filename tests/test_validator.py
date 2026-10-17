from backend.agent.narrative import NarrativeValidator

KNOWN = {"checkout", "payments-api", "cart", "search", "kafka"}
EVIDENCE = {"inc-1042", "dep-pay-1352", "checkout", "payments-api", "rb-pool-sizing"}


def test_accepts_narrative_grounded_in_evidence():
    v = NarrativeValidator(KNOWN, EVIDENCE, traversals_empty=False)
    ok, problems = v.validate("Best-supported explanation: dep-pay-1352 on payments-api; follow rb-pool-sizing. checkout is the symptom.")
    assert ok, problems


def test_rejects_invented_deployment_id():
    v = NarrativeValidator(KNOWN, EVIDENCE, traversals_empty=False)
    ok, problems = v.validate("The cause is dep-xyz-9999 on payments-api.")
    assert not ok and any("dep-xyz-9999" in p for p in problems)


def test_rejects_service_not_in_evidence():
    v = NarrativeValidator(KNOWN, EVIDENCE, traversals_empty=False)
    ok, problems = v.validate("kafka was overloaded and caused checkout to fail.")
    assert not ok and any("kafka" in p for p in problems)


def test_inconclusive_outcome_cannot_be_narrated_as_supported():
    v = NarrativeValidator(KNOWN, EVIDENCE, traversals_empty=False, outcome="inconclusive")
    ok, problems = v.validate("The payments-api hypothesis is supported.")
    assert not ok and any("inconclusive" in p for p in problems)
    ok, _ = v.validate("Inconclusive: the best-supported candidate on payments-api could not be confirmed; missing data: vendor status.")
    assert ok


def test_rejects_overclaim_and_impact_without_traversal():
    v = NarrativeValidator(KNOWN, EVIDENCE, traversals_empty=True)
    ok, problems = v.validate("This is the proven cause; it impacted checkout.")
    assert not ok
    assert any("over-claims" in p for p in problems)
    assert any("impact" in p for p in problems)
