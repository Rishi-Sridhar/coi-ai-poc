"""Reviewer actions, Use as Precedent, the Learning A -> B -> C demonstration, resubmission and reset."""
import pytest

from coi import db, demo
from coi.pipeline import resubmit_declaration, submit_declaration
from coi.review import CONFIRM, OVERRIDE, REQUEST_INFO, DecisionError, record_decision

A_RATIONALE = "Relative has no involvement, influence or access relevant to the opportunity."


def _submit(s, key):
    r = submit_declaration(s, demo.scenario_context(key), demo.scenario_answers(key))
    assert r.ok, r.errors
    return r.case_id


def test_override_requires_outcome_and_rationale(demo_settings):
    cid = _submit(demo_settings, "LEARNING_A")
    with pytest.raises(DecisionError, match="CONFLICT or NO CONFLICT"):
        record_decision(demo_settings, cid, OVERRIDE, "Rev", final_outcome=None, rationale="x")
    with pytest.raises(DecisionError, match="rationale is mandatory"):
        record_decision(demo_settings, cid, OVERRIDE, "Rev", final_outcome="NO CONFLICT", rationale="  ")
    assert db.get_case(demo_settings, cid)["status"] == "PENDING_REVIEW"


def test_confirm_maps_potential_to_conflict(demo_settings):
    cid = _submit(demo_settings, "POTENTIAL_CONFLICT")
    res = record_decision(demo_settings, cid, CONFIRM, "Rev", final_actions=["DISENGAGE_INDIVIDUAL"])
    assert res["final_outcome"] == "CONFLICT" and res["status"] == "CLOSED" and res["precedent_id"] is None
    d = db.one(demo_settings, "SELECT * FROM decisions WHERE case_id=?", (cid,))
    assert d["agrees_with_ai"] == 1 and d["use_as_precedent"] == 0


def test_confirm_not_allowed_for_insufficient(demo_settings):
    cid = _submit(demo_settings, "GIBBERISH")
    with pytest.raises(DecisionError):
        record_decision(demo_settings, cid, CONFIRM, "Rev")


def test_request_more_information(demo_settings):
    cid = _submit(demo_settings, "GIBBERISH")
    with pytest.raises(DecisionError):
        record_decision(demo_settings, cid, REQUEST_INFO, "Rev", info_fields=["RELATIVE_INVOLVEMENT_DETAILS"],
                        info_message="Please explain.", use_as_precedent=True, rationale="r")
    res = record_decision(demo_settings, cid, REQUEST_INFO, "Rev", info_fields=["RELATIVE_INVOLVEMENT_DETAILS"],
                          info_message="Please describe your parent's role in the tender.")
    assert res["status"] == "AWAITING_INFORMATION" and res["final_outcome"] is None
    events = [e["event_type"] for e in db.rows(demo_settings, "SELECT event_type FROM audit_events WHERE case_id=?", (cid,))]
    assert "INFO_REQUESTED" in events
    info_email = db.one(demo_settings, "SELECT * FROM emails WHERE case_id=? AND kind='INFO_REQUEST'", (cid,))
    assert info_email["recipient"] == "arjun.mehta@example.com"
    assert f"?page=submit&resubmit={cid}" in info_email["body_text"]
    assert db.list_precedents(demo_settings) and all(p["source"] == "SYNTHETIC" for p in db.list_precedents(demo_settings))

    # Respondent resubmits with a proper explanation -> new version, re-assessed, back to review
    answers = dict(demo.scenario_answers("GIBBERISH"), RELATIVE_INVOLVEMENT_DETAILS=(
        "My father chairs the technical evaluation committee for this smart-meter tender and scores all bids."))
    r = resubmit_declaration(demo_settings, cid, demo.scenario_context("GIBBERISH"), answers)
    assert r.ok and r.case_id == cid
    assert db.latest_submission(demo_settings, cid)["version"] == 2
    assert db.latest_assessment(demo_settings, cid)["verdict"] == "POTENTIAL CONFLICT IDENTIFIED"
    assert db.get_case(demo_settings, cid)["status"] == "PENDING_REVIEW"


def test_use_as_precedent_requires_rationale_and_is_anonymised(demo_settings):
    cid = _submit(demo_settings, "LEARNING_A")
    with pytest.raises(DecisionError, match="mandatory"):
        record_decision(demo_settings, cid, CONFIRM, "Rev", use_as_precedent=True, final_actions=["DISENGAGE_INDIVIDUAL"])
    res = record_decision(demo_settings, cid, OVERRIDE, "Rev", final_outcome="NO CONFLICT", rationale=A_RATIONALE,
                          use_as_precedent=True, final_actions=["NO_FURTHER_ACTION"])
    assert res["precedent_id"] == "REV-001"
    (p,) = [p for p in db.list_precedents(demo_settings) if p["source"] == "REVIEWER"]
    assert p["outcome"] == "NO CONFLICT" and p["case_id"] == cid and p["active"] == 1 and p["guidance_version"] == "v1"
    assert "Rahul" not in p["snapshot_json"] and "rahul.verma@example.com" not in p["snapshot_json"]
    assert p["snapshot"]["answers"]["RELATIVE_INFLUENCE"] == "No"


def test_learning_a_b_c(demo_settings):
    s = demo_settings
    a = _submit(s, "LEARNING_A")
    assert db.latest_assessment(s, a)["verdict"] == "POTENTIAL CONFLICT IDENTIFIED"  # conservative guidance
    record_decision(s, a, OVERRIDE, "Rev", final_outcome="NO CONFLICT", rationale=A_RATIONALE, use_as_precedent=True,
                    final_actions=["NO_FURTHER_ACTION"])

    b = _submit(s, "LEARNING_B")
    ab = db.latest_assessment(s, b)
    assert ab["precedents"][0]["precedent_id"] == "REV-001" and ab["precedents"][0]["score"] == 1.0
    assert ab["verdict"] == "NO CONFLICT APPARENT"
    rel = {p["precedent_id"]: p["relationship_to_current_case"] for p in ab["result"]["precedent_analysis"]}
    assert rel["REV-001"] == "APPLIED"

    c = _submit(s, "LEARNING_C")
    ac = db.latest_assessment(s, c)
    assert "REV-001" in [p["precedent_id"] for p in ac["precedents"]], "Case A must still be retrieved for Case C"
    assert ac["verdict"] == "POTENTIAL CONFLICT IDENTIFIED"
    pa = {p["precedent_id"]: p for p in ac["result"]["precedent_analysis"]}
    assert pa["REV-001"]["relationship_to_current_case"] == "DISTINGUISHED"
    assert "decision-making authority" in pa["REV-001"]["material_fact"]


def test_retired_precedent_not_retrieved(demo_settings):
    from coi.review import set_precedent_active
    s = demo_settings
    a = _submit(s, "LEARNING_A")
    record_decision(s, a, OVERRIDE, "Rev", final_outcome="NO CONFLICT", rationale=A_RATIONALE, use_as_precedent=True,
                    final_actions=["NO_FURTHER_ACTION"])
    set_precedent_active(s, "REV-001", False, "Rev", "test")
    b = _submit(s, "LEARNING_B")
    assert "REV-001" not in [p["precedent_id"] for p in db.latest_assessment(s, b)["precedents"]]


def test_reset_restores_predictable_state(demo_settings):
    s = demo_settings
    a = _submit(s, "LEARNING_A")
    record_decision(s, a, OVERRIDE, "Rev", final_outcome="NO CONFLICT", rationale=A_RATIONALE, use_as_precedent=True,
                    final_actions=["NO_FURTHER_ACTION"])
    from coi.review import set_precedent_active
    set_precedent_active(s, "SYN-001", False, "Rev")
    db.set_setting(s, "simulate_ai_failure", "true")
    db.reset_demo(s)
    assert db.rows(s, "SELECT * FROM cases") == [] and db.rows(s, "SELECT * FROM emails") == []
    precs = db.list_precedents(s)
    assert len(precs) == 15 and all(p["source"] == "SYNTHETIC" and p["active"] for p in precs)
    assert not db.simulate_ai_failure(s)
    assert _submit(s, "CLEAN") == "COI-0001"
