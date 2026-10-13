"""Recommended actions: controlled catalogue, deterministic rules, guardrails, reviewer final action, learning, email."""
import json
import os

import pytest

from coi import db, demo, pipeline, review
from coi.actions import (action_codes, action_label, deterministic_actions, load_catalog, merge_actions,
                         recommended_actions, validate_ai_actions, validate_final_actions)
from coi.guardrails import validate_assessment
from coi.retrieval import llm_view, retrieve
from conftest import ALL_NO, CONTEXT, FakeLLM, assessment_result, make_settings

NO_ISSUES = {"issues": [], "summary": "none"}
A_RATIONALE = "Relative has no involvement, influence or access relevant to the opportunity."


def _codes(actions):
    return [a["action_code"] for a in actions]


def _submit(s, key, **kw):
    r = pipeline.submit_declaration(s, demo.scenario_context(key), demo.scenario_answers(key), **kw)
    assert r.ok, r.errors
    return r.case_id


def _actions_for(s, cid):
    return _codes(recommended_actions(db.latest_assessment(s, cid)))


# ---------------------------------------------------------------- catalogue

def test_catalogue_has_the_ten_controlled_actions_and_disclaimer():
    assert action_codes() == ["NO_FURTHER_ACTION", "REQUEST_ADDITIONAL_INFORMATION", "DISENGAGE_INDIVIDUAL",
                              "RECUSE_FROM_DECISION_MAKING", "ESTABLISH_INFORMATION_BARRIER", "RESTRICT_INFORMATION_USE",
                              "HOLD_OPPORTUNITY", "OBTAIN_REQUIRED_DECLARATION", "ESCALATE_COMPLIANCE",
                              "ESCALATE_LEGAL_COMPLIANCE"]
    assert action_label("DISENGAGE_INDIVIDUAL") == "Disengage Individual from Opportunity"
    assert "Synthetic demonstration guidance" in load_catalog()["disclaimer"]


# ---------------------------------------------------------------- 1–5 AI action guardrails

def _act(code, reason="The respondent's spouse evaluates vendors for this tender."):
    return {"action_code": code, "reason": reason}


@pytest.mark.parametrize("actions, verdict, fragment", [
    ([_act("FIRE_EVERYONE")], "POTENTIAL CONFLICT IDENTIFIED", "unknown action code"),                    # 1
    ([_act("ESCALATE_COMPLIANCE"), _act("ESCALATE_COMPLIANCE")], "POTENTIAL CONFLICT IDENTIFIED", "duplicate"),  # 2
    ([_act("ESCALATE_COMPLIANCE", "  ")], "POTENTIAL CONFLICT IDENTIFIED", "has no reason"),               # 3
    ([_act("DISENGAGE_INDIVIDUAL", "Because a conflict exists.")], "POTENTIAL CONFLICT IDENTIFIED", "not tied"),
    ([_act("NO_FURTHER_ACTION"), _act("DISENGAGE_INDIVIDUAL")], "NO CONFLICT APPARENT", "cannot be combined"),  # 4
    ([_act("ESCALATE_COMPLIANCE")], "INSUFFICIENT INFORMATION", "must recommend Request Additional Information"),  # 5
    ([], "NO CONFLICT APPARENT", "missing or empty"),
    ([_act("ESCALATE_COMPLIANCE", "See answer RELATIVE_INFLUENCE for details of the relative's role.")],
     "POTENTIAL CONFLICT IDENTIFIED", "cites answers not in the declaration"),
])
def test_ai_action_guardrails_reject(actions, verdict, fragment):
    errors = validate_ai_actions(actions, verdict, ALL_NO)
    assert any(fragment in e for e in errors), errors


def test_valid_ai_actions_pass():
    assert validate_ai_actions([_act("DISENGAGE_INDIVIDUAL"), _act("ESCALATE_COMPLIANCE")],
                               "POTENTIAL CONFLICT IDENTIFIED", ALL_NO) == []


@pytest.mark.parametrize("bad_actions", [
    [_act("FIRE_EVERYONE")],                                          # unknown code (schema enum + G8)
    [_act("NO_FURTHER_ACTION"), _act("DISENGAGE_INDIVIDUAL")],        # conflicting combination
])
def test_action_guardrail_failure_retries_then_fails_safe_never_clears(settings, bad_actions):
    llm = FakeLLM(quality=[NO_ISSUES], assessment=[assessment_result(actions=bad_actions)])
    r = pipeline.submit_declaration(settings, CONTEXT, ALL_NO, llm_client=llm)
    a = db.latest_assessment(settings, r.case_id)
    assert a["verdict"] == "AI ASSESSMENT UNAVAILABLE" and r.review_required
    assert len([x for x in a["guardrails"]["attempts"] if x["step"] == "assessment"]) == 2
    assert _codes(recommended_actions(a)) == ["ESCALATE_COMPLIANCE"]  # rule A2: manual Compliance assessment


def test_insufficient_after_g5_downgrade_gets_request_and_drops_no_further_action(settings):
    issue = {"question_id": "RELATIVE_INVOLVEMENT_DETAILS", "submitted_text": "asdfgh", "issue_type": "GIBBERISH",
             "explanation": "Random characters.", "missing_elements": [], "more_info_required": True,
             "follow_up_question": "Please describe whether your parent can influence the tender."}
    answers = dict(ALL_NO, RELATIVE_AT_SOE="Yes", RELATIVE_INFLUENCE="Yes", RELATIVE_INVOLVEMENT_DETAILS="asdfgh",
                   RELATIVE_RELATIONSHIP="Parent", RELATIVE_DESIGNATION="Engineer, Grid Operations")
    llm = FakeLLM(quality=[{"issues": [issue], "summary": ""}], assessment=[assessment_result()])  # model says NFA
    r = pipeline.submit_declaration(settings, CONTEXT, answers, llm_client=llm)
    a = db.latest_assessment(settings, r.case_id)
    assert a["verdict"] == "INSUFFICIENT INFORMATION"
    acts = recommended_actions(a)
    assert _codes(acts) == ["REQUEST_ADDITIONAL_INFORMATION"]
    assert acts[0]["source"] == "rule" and "influence the tender" in acts[0]["reason"]


# ---------------------------------------------------------------- 6 deterministic actions

def test_deterministic_intermediary_actions():
    answers = dict(ALL_NO, INTERMEDIARY_INVOLVED="Yes", INTERMEDIARY_DECLARATION="No")
    assert _codes(deterministic_actions(answers, [], ai_failed=False)) == ["OBTAIN_REQUIRED_DECLARATION", "HOLD_OPPORTUNITY"]
    answers = dict(ALL_NO, INTERMEDIARY_INVOLVED="Yes", INTERMEDIARY_DECLARATION="Yes")  # obtained but no file stored
    assert "OBTAIN_REQUIRED_DECLARATION" in _codes(deterministic_actions(answers, [], ai_failed=False))
    ok = dict(answers, INTERMEDIARY_DECLARATION_FILE="decl.pdf")
    assert deterministic_actions(ok, [], ai_failed=False) == []


def test_deterministic_actions_always_appear_and_ai_failure_escalates():
    merged = merge_actions(deterministic_actions(dict(ALL_NO, INTERMEDIARY_INVOLVED="Yes"), [], ai_failed=False),
                           [_act("NO_FURTHER_ACTION")], "NO CONFLICT APPARENT")
    assert _codes(merged) == ["OBTAIN_REQUIRED_DECLARATION", "HOLD_OPPORTUNITY"]  # NFA dropped, rule actions kept
    assert _codes(deterministic_actions(ALL_NO, [], ai_failed=True)) == ["ESCALATE_COMPLIANCE"]
    issue = {"question_id": "OTHER_CONFLICT_DETAILS", "follow_up_question": "Describe the conflict."}
    assert _codes(deterministic_actions(ALL_NO, [issue], ai_failed=False)) == ["REQUEST_ADDITIONAL_INFORMATION"]


def test_intermediary_submission_block_unchanged(demo_settings):
    r = pipeline.submit_declaration(demo_settings, demo.scenario_context("INTERMEDIARY"), demo.scenario_answers("INTERMEDIARY"))
    assert not r.ok and db.rows(demo_settings, "SELECT case_id FROM cases") == []


# ---------------------------------------------------------------- 7–9 scenario recommendations (demo mode)

@pytest.mark.parametrize("key, verdict, expected", [
    ("CLEAN", "NO CONFLICT APPARENT", ["NO_FURTHER_ACTION"]),
    ("LEARNING_C", "POTENTIAL CONFLICT IDENTIFIED", ["DISENGAGE_INDIVIDUAL"]),
    ("POTENTIAL_CONFLICT", "POTENTIAL CONFLICT IDENTIFIED", ["DISENGAGE_INDIVIDUAL"]),  # respondent is not the owner
    ("PRIOR_CONFIDENTIAL", "POTENTIAL CONFLICT IDENTIFIED", ["ESTABLISH_INFORMATION_BARRIER", "DISENGAGE_INDIVIDUAL"]),
    ("IMPROPER_INFO", "POTENTIAL CONFLICT IDENTIFIED", ["RESTRICT_INFORMATION_USE", "ESTABLISH_INFORMATION_BARRIER", "ESCALATE_LEGAL_COMPLIANCE"]),
    ("PASSIVE_INTEREST", "NO CONFLICT APPARENT", ["NO_FURTHER_ACTION"]),
    ("GIBBERISH", "INSUFFICIENT INFORMATION", ["REQUEST_ADDITIONAL_INFORMATION"]),
    ("INSUFFICIENT", "INSUFFICIENT INFORMATION", ["REQUEST_ADDITIONAL_INFORMATION"]),
    ("CONTRADICTION", "POTENTIAL CONFLICT IDENTIFIED", ["REQUEST_ADDITIONAL_INFORMATION"]),
])
def test_scenario_recommendations(demo_settings, key, verdict, expected):
    cid = _submit(demo_settings, key)
    a = db.latest_assessment(demo_settings, cid)
    assert a["verdict"] == verdict
    assert _actions_for(demo_settings, cid) == expected
    for act in recommended_actions(a):
        assert len(act["reason"]) > 20 and not act["reason"].lower().startswith("because a conflict")


def test_family_influence_reason_cites_the_facts(demo_settings):
    cid = _submit(demo_settings, "LEARNING_C")
    disengage = next(x for x in recommended_actions(db.latest_assessment(demo_settings, cid))
                     if x["action_code"] == "DISENGAGE_INDIVIDUAL")
    assert "sibling" in disengage["reason"] and "tender evaluation committee" in disengage["reason"]


# ---------------------------------------------------------------- 10–13 reviewer final action and learning

def test_final_action_required_and_persisted(demo_settings):
    s = demo_settings
    cid = _submit(s, "LEARNING_C")
    with pytest.raises(review.DecisionError, match="final action"):
        review.record_decision(s, cid, review.CONFIRM, "Rev")
    with pytest.raises(review.DecisionError, match="No Further Action"):
        review.record_decision(s, cid, review.CONFIRM, "Rev", final_actions=["NO_FURTHER_ACTION", "DISENGAGE_INDIVIDUAL"])
    accepted = _actions_for(s, cid)
    res = review.record_decision(s, cid, review.CONFIRM, "Rev", final_actions=accepted, final_action_notes="Effective today.")
    d = db.one(s, "SELECT * FROM decisions WHERE id=?", (res["decision_id"],))
    assert json.loads(d["final_action_codes"]) == accepted and d["final_action_notes"] == "Effective today."
    assert validate_final_actions(accepted, "CONFLICT") == []


def test_reviewer_can_modify_recommended_actions(demo_settings):
    s = demo_settings
    cid = _submit(s, "IMPROPER_INFO")
    modified = ["RESTRICT_INFORMATION_USE", "DISENGAGE_INDIVIDUAL"]
    res = review.record_decision(s, cid, review.CONFIRM, "Rev", final_actions=modified)
    assert res["final_actions"] == modified != _actions_for(s, cid)
    d = db.one(s, "SELECT final_action_codes FROM decisions WHERE case_id=?", (cid,))
    assert json.loads(d["final_action_codes"]) == modified


def test_request_more_information_records_request_action_only(demo_settings):
    s = demo_settings
    cid = _submit(s, "GIBBERISH")
    res = review.record_decision(s, cid, review.REQUEST_INFO, "Rev", info_fields=["RELATIVE_INVOLVEMENT_DETAILS"],
                                 info_message="Please explain.")
    assert res["status"] == "AWAITING_INFORMATION" and res["final_actions"] == ["REQUEST_ADDITIONAL_INFORMATION"]


def test_human_final_action_stored_in_reference_case_and_used_by_future_cases(demo_settings):
    s = demo_settings
    a_id = _submit(s, "LEARNING_A")
    assert _actions_for(s, a_id) == ["REQUEST_ADDITIONAL_INFORMATION"]  # verify; no disengagement for a relative alone
    review.record_decision(s, a_id, review.OVERRIDE, "Rev", final_outcome="NO CONFLICT", rationale=A_RATIONALE,
                           use_as_precedent=True, final_actions=["NO_FURTHER_ACTION"])
    (prec,) = [p for p in db.list_precedents(s) if p["source"] == "REVIEWER"]
    assert prec["snapshot"]["final_actions"] == ["NO_FURTHER_ACTION"]                      # 12
    b_id = _submit(s, "LEARNING_B")
    b = db.latest_assessment(s, b_id)
    assert b["verdict"] == "NO CONFLICT APPARENT"
    (act,) = recommended_actions(b)
    assert act["action_code"] == "NO_FURTHER_ACTION" and "REV-001" in act["reason"]        # 13
    current = {"answers": demo.scenario_answers("LEARNING_B"), "is_owner": "No", "soe_relationship": "Client"}
    view = {p["precedent_id"]: p for p in llm_view(retrieve(current, db.list_precedents(s, active_only=True)))}
    assert view["REV-001"]["final_actions"] == ["NO_FURTHER_ACTION"]
    c_id = _submit(s, "LEARNING_C")                                                          # 17: C still flagged
    assert db.latest_assessment(s, c_id)["verdict"] == "POTENTIAL CONFLICT IDENTIFIED"
    assert "DISENGAGE_INDIVIDUAL" in _actions_for(s, c_id)


def test_llm_receives_catalogue_and_reference_final_actions(settings):
    llm = FakeLLM(quality=[NO_ISSUES], assessment=[assessment_result()])
    pipeline.submit_declaration(settings, CONTEXT, ALL_NO, llm_client=llm)
    payload = json.loads(llm.calls[-1].split("<case_data>")[1].split("</case_data>")[0])
    assert [x["action_code"] for x in payload["action_catalogue"]] == action_codes()
    assert all("final_actions" in p for p in payload["precedents"])
    assert payload["deterministic_facts"]["required_actions"] == []


def test_g8_in_validate_assessment():
    bad = assessment_result("POTENTIAL CONFLICT IDENTIFIED", actions=[_act("ESCALATE_COMPLIANCE"), _act("ESCALATE_COMPLIANCE")],
                            contributing=[{"question_id": "RELATIVE_AT_SOE", "submitted_value": "No", "explanation": "x"}],
                            precedents=[])
    assert any(e.startswith("G8") for e in validate_assessment(bad, ALL_NO, [], []))


# ---------------------------------------------------------------- 14 email

def test_email_shows_business_action_label_and_reason(demo_settings):
    s = demo_settings
    cid = _submit(s, "IMPROPER_INFO")
    e = db.one(s, "SELECT * FROM emails WHERE case_id=?", (cid,))
    for part in (e["body_html"], e["body_text"]):
        assert "Recommended action" in part or "RECOMMENDED ACTION" in part
        assert "Do Not Use / Restrict Access to Information" in part and "Escalate to Compliance / Legal" in part
        assert "draft fee proposal" in part  # reason tied to the submitted facts
        assert not any(code in part for code in action_codes())  # no internal action codes
        assert "final mitigation decision remains with the Compliance reviewer" in part


def test_clean_confirmation_email_recommends_no_further_action(tmp_path):
    s = make_settings(tmp_path, demo_mode=True, groq_api_key="", review_clean_cases=True)
    cid = _submit(s, "CLEAN")
    body = db.one(s, "SELECT body_text FROM emails WHERE case_id=?", (cid,))["body_text"]
    assert "RECOMMENDED ACTION\n  No Further Action" in body
    assert "No additional mitigation is recommended based on the information provided" in body


def test_email_for_inadequate_information_recommends_request(demo_settings):
    cid = _submit(demo_settings, "INSUFFICIENT")
    body = db.one(demo_settings, "SELECT body_text FROM emails WHERE case_id=?", (cid,))["body_text"]
    assert "RECOMMENDED ACTION\n  Request Additional Information" in body and "Please clarify your answer to Q1a" in body


# ---------------------------------------------------------------- 15 result card (rendered via Streamlit AppTest)

def test_result_card_displays_recommended_actions(tmp_path, monkeypatch):
    from streamlit.testing.v1 import AppTest
    monkeypatch.setenv("COI_DB_PATH", str(tmp_path / "ui.db"))
    monkeypatch.setenv("COI_UPLOADS_DIR", str(tmp_path / "up"))
    monkeypatch.setenv("DEMO_MODE", "true")
    monkeypatch.setenv("GROQ_API_KEY", "")
    monkeypatch.setenv("SMTP_HOST", "")
    at = AppTest.from_file(os.path.join(os.path.dirname(__file__), "..", "app.py"), default_timeout=60)
    at.run()
    titles = list(demo.scenarios())
    at.selectbox(key="scenario_pick").select_index(titles.index("PRIOR_CONFIDENTIAL"))
    at.button[[b.label for b in at.button].index("Load")].click().run()
    at.button[[b.label for b in at.button].index("Submit for Assessment")].click().run()
    assert not at.exception
    md = " ".join(m.value for m in at.markdown)
    assert "Recommended action" in md and "Establish Information Barrier" in md
    assert "Disengage Individual from Opportunity" in md and "Escalate to Compliance" not in md  # workflow, not a card
    assert "ESTABLISH_INFORMATION_BARRIER" not in md  # business labels only on the result card
