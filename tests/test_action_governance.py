"""Action governance: final outcome vs final action, workflow-only actions, and fact-supported recommendations."""
import pytest
import yaml

from coi import db, demo, fallback, pipeline, review
from coi.actions import (HOLD_FALLBACK_REASON, display_actions, final_action_options, merge_actions,
                         recommended_actions, validate_ai_actions, validate_final_actions)
from coi.config import CONFIG_DIR
from conftest import ALL_NO

A_RATIONALE = "Relative has no involvement, influence or access relevant to the opportunity."


def _submit(s, key):
    r = pipeline.submit_declaration(s, demo.scenario_context(key), demo.scenario_answers(key))
    assert r.ok, r.errors
    return r.case_id


# ---------------------------------------------------------------- outcome / final action consistency (pure rule)

@pytest.mark.parametrize("outcome, codes, ok", [
    ("NO CONFLICT", ["NO_FURTHER_ACTION"], True),
    ("NO CONFLICT", ["DISENGAGE_INDIVIDUAL"], False),
    ("NO CONFLICT", ["NO_FURTHER_ACTION", "ESTABLISH_INFORMATION_BARRIER"], False),
    ("CONFLICT", ["NO_FURTHER_ACTION"], False),
    ("CONFLICT", ["DISENGAGE_INDIVIDUAL"], True),
    ("CONFLICT", ["RESTRICT_INFORMATION_USE", "ESCALATE_LEGAL_COMPLIANCE"], True),  # specialist escalation stays available
    ("CONFLICT", ["REQUEST_ADDITIONAL_INFORMATION"], False),  # only via the Request More Information workflow
    ("CONFLICT", ["ESCALATE_COMPLIANCE"], False),             # workflow / routing, not a final action
    ("CONFLICT", ["DISENGAGE_INDIVIDUAL", "ESCALATE_COMPLIANCE"], False),
    ("NO CONFLICT", ["REQUEST_ADDITIONAL_INFORMATION"], False),
    ("CONFLICT", [], False),
])
def test_final_outcome_and_final_action_consistency(outcome, codes, ok):
    assert (validate_final_actions(codes, outcome) == []) is ok


def test_selectable_final_actions_depend_on_outcome():
    assert final_action_options("NO CONFLICT") == ["NO_FURTHER_ACTION"]
    conflict = final_action_options("CONFLICT")
    assert "NO_FURTHER_ACTION" not in conflict and "REQUEST_ADDITIONAL_INFORMATION" not in conflict
    assert "ESCALATE_COMPLIANCE" not in conflict and "ESCALATE_LEGAL_COMPLIANCE" in conflict
    assert {"DISENGAGE_INDIVIDUAL", "RECUSE_FROM_DECISION_MAKING", "ESTABLISH_INFORMATION_BARRIER",
            "RESTRICT_INFORMATION_USE", "HOLD_OPPORTUNITY", "OBTAIN_REQUIRED_DECLARATION"} <= set(conflict)


# ---------------------------------------------------------------- invalid combinations blocked before persistence

@pytest.mark.parametrize("key, action, outcome, codes", [
    ("LEARNING_A", review.OVERRIDE, "NO CONFLICT", ["DISENGAGE_INDIVIDUAL"]),      # No Conflict + mitigation
    ("LEARNING_C", review.CONFIRM, None, ["NO_FURTHER_ACTION"]),                  # Conflict + No Further Action
    ("LEARNING_C", review.CONFIRM, None, ["REQUEST_ADDITIONAL_INFORMATION"]),     # info request as closed final action
    ("LEARNING_C", review.CONFIRM, None, ["ESCALATE_COMPLIANCE"]),                # workflow as final action
    ("LEARNING_A", review.OVERRIDE, "NO CONFLICT", ["NO_FURTHER_ACTION", "ESCALATE_LEGAL_COMPLIANCE"]),
])
def test_invalid_combinations_blocked_before_persistence_and_never_become_reference_cases(demo_settings, key, action,
                                                                                          outcome, codes):
    s = demo_settings
    cid = _submit(s, key)
    with pytest.raises(review.DecisionError):
        review.record_decision(s, cid, action, "Rev", final_outcome=outcome, rationale=A_RATIONALE,
                               use_as_precedent=True, final_actions=codes)
    assert db.rows(s, "SELECT id FROM decisions WHERE case_id=?", (cid,)) == []           # nothing persisted
    assert db.get_case(s, cid)["status"] == "PENDING_REVIEW"
    assert [p for p in db.list_precedents(s) if p["source"] == "REVIEWER"] == []         # no reference case
    assert not db.rows(s, "SELECT id FROM audit_events WHERE case_id=? AND event_type IN "
                          "('DECISION_RECORDED', 'PRECEDENT_ADDED')", (cid,))


def test_valid_combinations_are_recorded_and_can_become_reference_cases(demo_settings):
    s = demo_settings
    a_id = _submit(s, "LEARNING_A")
    res = review.record_decision(s, a_id, review.OVERRIDE, "Rev", final_outcome="NO CONFLICT", rationale=A_RATIONALE,
                                 use_as_precedent=True, final_actions=["NO_FURTHER_ACTION"])
    assert res["precedent_id"] == "REV-001"
    c_id = _submit(s, "IMPROPER_INFO")
    res = review.record_decision(s, c_id, review.CONFIRM, "Rev", rationale="Competitor pricing must not be used.",
                                 use_as_precedent=True, final_actions=["RESTRICT_INFORMATION_USE", "ESCALATE_LEGAL_COMPLIANCE"])
    assert res["final_outcome"] == "CONFLICT" and res["precedent_id"] == "REV-002"


def test_request_more_information_remains_the_only_route_for_request_action(demo_settings):
    s = demo_settings
    cid = _submit(s, "GIBBERISH")
    res = review.record_decision(s, cid, review.REQUEST_INFO, "Rev", info_fields=["RELATIVE_INVOLVEMENT_DETAILS"],
                                 info_message="Please explain.", final_actions=["DISENGAGE_INDIVIDUAL"])  # ignored
    assert res["final_actions"] == ["REQUEST_ADDITIONAL_INFORMATION"] and res["status"] == "AWAITING_INFORMATION"


def test_synthetic_reference_cases_have_valid_final_actions():
    seeds = yaml.safe_load((CONFIG_DIR / "synthetic_precedents.yaml").read_text(encoding="utf-8"))["precedents"]
    for seed in seeds:
        assert validate_final_actions(seed["final_actions"], seed["outcome"]) == [], seed["id"]


# ---------------------------------------------------------------- Escalate to Compliance is workflow, not a card

def test_escalate_to_compliance_hidden_from_business_recommendations():
    actions = [{"action_code": "ESCALATE_COMPLIANCE", "reason": "x"}, {"action_code": "DISENGAGE_INDIVIDUAL", "reason": "y"},
               {"action_code": "ESCALATE_LEGAL_COMPLIANCE", "reason": "z"}]
    assert [a["action_code"] for a in display_actions(actions)] == ["DISENGAGE_INDIVIDUAL", "ESCALATE_LEGAL_COMPLIANCE"]


def test_ai_unavailable_routes_to_compliance_review_without_a_recommendation_card(demo_settings):
    s = demo_settings
    db.set_setting(s, "simulate_ai_failure", "true")
    cid = _submit(s, "POTENTIAL_CONFLICT")
    a = db.latest_assessment(s, cid)
    assert [x["action_code"] for x in recommended_actions(a)] == ["ESCALATE_COMPLIANCE"]  # kept internally (routing)
    assert display_actions(recommended_actions(a)) == []
    body = db.one(s, "SELECT body_text FROM emails WHERE case_id=?", (cid,))["body_text"]
    assert "RECOMMENDED ACTION" not in body and "Manual Review Required" in body


def test_email_never_recommends_escalate_to_compliance(demo_settings):
    s = demo_settings
    for key in ("LEARNING_C", "PRIOR_CONFIDENTIAL", "CONTRADICTION", "POTENTIAL_CONFLICT"):
        cid = _submit(s, key)
        body = db.one(s, "SELECT body_text FROM emails WHERE case_id=?", (cid,))["body_text"]
        assert "Escalate to Compliance\n" not in body and "Also: Escalate to Compliance —" not in body, key


# ---------------------------------------------------------------- recommendations supported by the facts

def _family_influence(owner: bool):
    answers = dict(ALL_NO, RELATIVE_AT_SOE="Yes", RELATIVE_INFLUENCE="Yes", RELATIVE_RELATIONSHIP="Spouse",
                   RELATIVE_DESIGNATION="Director, Procurement department",
                   RELATIVE_INVOLVEMENT_DETAILS="My spouse sits on the tender evaluation committee and selects vendors.")
    return fallback.assess_case(answers, [], [], is_owner=owner)["recommended_actions"]


@pytest.mark.parametrize("owner", [False, True])
def test_direct_family_influence_recommends_disengagement_only(owner):
    codes = [a["action_code"] for a in _family_influence(owner)]
    assert codes == ["DISENGAGE_INDIVIDUAL"]  # no automatic recusal; Compliance review is the next step


def _third_party(owner: bool):
    answers = dict(ALL_NO, THIRD_PARTY_INTEREST="Yes", THIRD_PARTY_SERVICES_TO="State-Owned Entity",
                   THIRD_PARTY_NAME="Bayline Marine Services Ltd", THIRD_PARTY_REG_NO="BMS-1",
                   THIRD_PARTY_SERVICES_NATURE="Dredging services to the port authority for the terminal in scope.",
                   THIRD_PARTY_OWNERSHIP="35% shareholding and board seat", THIRD_PARTY_RELATIONSHIP="Sibling")
    return [a["action_code"] for a in fallback.assess_case(answers, [], [], is_owner=owner)["recommended_actions"]]


def test_recusal_only_when_the_respondent_has_a_stated_decision_role():
    assert _third_party(owner=False) == ["DISENGAGE_INDIVIDUAL"]
    assert _third_party(owner=True) == ["RECUSE_FROM_DECISION_MAKING"]  # Opportunity Owner = decision-making role


def test_fallback_never_recommends_escalate_to_compliance_alongside_a_specific_mitigation(demo_settings):
    for key in ("LEARNING_C", "POTENTIAL_CONFLICT", "PRIOR_CONFIDENTIAL", "IMPROPER_INFO", "CONTRADICTION"):
        codes = [x["action_code"] for x in recommended_actions(db.latest_assessment(demo_settings, _submit(demo_settings, key)))]
        assert "ESCALATE_COMPLIANCE" not in codes, (key, codes)


# ---------------------------------------------------------------- recommendation completeness

def _g8(verdict, *codes):
    actions = [{"action_code": c, "reason": "Reason tied to the disclosed facts of this declaration."} for c in codes]
    return validate_ai_actions(actions, verdict, dict(ALL_NO))


@pytest.mark.parametrize("verdict, codes, ok", [
    ("POTENTIAL CONFLICT IDENTIFIED", ["ESCALATE_COMPLIANCE"], False),   # workflow alone is not a recommendation
    ("POTENTIAL CONFLICT IDENTIFIED", ["HOLD_OPPORTUNITY"], True),
    ("POTENTIAL CONFLICT IDENTIFIED", ["DISENGAGE_INDIVIDUAL"], True),
    ("POTENTIAL CONFLICT IDENTIFIED", ["REQUEST_ADDITIONAL_INFORMATION"], True),
    ("INSUFFICIENT INFORMATION", ["REQUEST_ADDITIONAL_INFORMATION"], True),
    ("NO CONFLICT APPARENT", ["NO_FURTHER_ACTION"], True),
])
def test_potential_conflict_needs_a_business_visible_recommendation(verdict, codes, ok):
    errors = _g8(verdict, *codes)
    assert (errors == []) is ok, errors
    if not ok:
        assert any("business-visible" in e for e in errors)


def test_merge_adds_hold_when_a_potential_conflict_has_only_workflow_actions():
    merged = merge_actions([{"action_code": "ESCALATE_COMPLIANCE", "reason": "Manual review.", "source": "rule"}], [],
                           "POTENTIAL CONFLICT IDENTIFIED")
    assert [(a["action_code"], a["reason"]) for a in display_actions(merged)] == [("HOLD_OPPORTUNITY", HOLD_FALLBACK_REASON)]


Q5_DETAILS = "I own 20% of a company that is bidding for a subcontract on this opportunity."


def test_generic_other_conflict_produces_a_visible_action():
    out = fallback.assess_case(dict(ALL_NO, OTHER_CONFLICT="Yes", OTHER_CONFLICT_DETAILS=Q5_DETAILS), [], [])
    assert out["verdict"] == "POTENTIAL CONFLICT IDENTIFIED"
    visible = display_actions(out["recommended_actions"])
    assert [a["action_code"] for a in visible] == ["HOLD_OPPORTUNITY"] and visible[0]["reason"] == HOLD_FALLBACK_REASON
    assert validate_ai_actions(out["recommended_actions"], out["verdict"], dict(ALL_NO, OTHER_CONFLICT="Yes")) == []


def test_generic_other_conflict_end_to_end_shows_hold_in_result_and_email(demo_settings):
    s = demo_settings
    ctx = demo.scenario_context("CLEAN")
    r = pipeline.submit_declaration(s, ctx, dict(ALL_NO, OTHER_CONFLICT="Yes", OTHER_CONFLICT_DETAILS=Q5_DETAILS))
    assert r.ok, r.errors
    a = db.latest_assessment(s, r.case_id)
    assert a["verdict"] == "POTENTIAL CONFLICT IDENTIFIED"
    assert [x["action_code"] for x in display_actions(recommended_actions(a))] == ["HOLD_OPPORTUNITY"]
    body = db.one(s, "SELECT body_text FROM emails WHERE case_id=?", (r.case_id,))["body_text"]
    assert "Place Opportunity on Hold" in body
