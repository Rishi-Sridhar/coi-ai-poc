"""Business UX refinement: reviewer email content, new inadequate-response scenarios, mitigating information."""
import re

import pytest

from coi import db, demo, pipeline, review
from coi.display import other_relevant_items
from coi.questionnaire import load_questionnaire

INTERNAL_IDS = re.compile(r"\b(" + "|".join(load_questionnaire()["by_id"]) + r")\b")
TECHNICAL = ("routing", "Routing", "precedent", "Precedent", "Similarity", "APPLIED", "DISTINGUISHED", "LLM", "guardrail",
             "INSUFFICIENT INFORMATION", "POTENTIAL CONFLICT IDENTIFIED", "NO CONFLICT APPARENT", "AI ASSESSMENT UNAVAILABLE",
             "GIBBERISH", "IRRELEVANT", "CONTRADICTORY", "NOT_CONFIGURED", "PENDING_REVIEW")


def _submit(s, key):
    r = pipeline.submit_declaration(s, demo.scenario_context(key), demo.scenario_answers(key))
    assert r.ok, r.errors
    return r.case_id


def _email(s, cid, kind="REVIEWER_NOTIFICATION"):
    return db.one(s, "SELECT * FROM emails WHERE case_id=? AND kind=? ORDER BY id DESC LIMIT 1", (cid, kind))


def _assert_business_only(email):
    for part in (email["body_html"], email["body_text"]):
        assert not INTERNAL_IDS.search(part), INTERNAL_IDS.search(part).group(0)
        leaked = [t for t in TECHNICAL if t in part]
        assert not leaked, leaked


def test_reviewer_email_is_a_business_communication(demo_settings):
    s = demo_settings
    cid = _submit(s, "LEARNING_C")
    e = _email(s, cid)
    ctx, answers = demo.scenario_context("LEARNING_C"), demo.scenario_answers("LEARNING_C")
    assert e["subject"] == f"[COI Review Required] {cid} – Potential Conflict Identified"
    for expected in ("Potential Conflict Identified", "Preliminary assessment", ctx["respondent_name"], ctx["opportunity_id"],
                     ctx["opportunity_name"], ctx["client"], "Submitted", answers["RELATIVE_INVOLVEMENT_DETAILS"],
                     "Why this was flagged", "Next action", "Compliance Review Required", "Open Declaration for Review"):
        assert expected in e["body_html"] or expected in e["body_text"], expected
    assert f'href="http://localhost:8501/?page=reviewer&amp;case={cid}" target="_blank"' in e["body_html"]
    assert f"?page=reviewer&case={cid}" in e["body_text"]
    _assert_business_only(e)


@pytest.mark.parametrize("key, issue_type, clarification_hint", [
    ("GIBBERISH", "GIBBERISH", "Please clarify your answer to Q1a"),
    ("IRRELEVANT", "IRRELEVANT", "Please clarify your answer to Q1a"),
    ("INSUFFICIENT", "INSUFFICIENT", "Please clarify your answer to Q1a"),
])
def test_inadequate_response_scenarios_and_emails(demo_settings, key, issue_type, clarification_hint):
    s = demo_settings
    cid = _submit(s, key)
    a = db.latest_assessment(s, cid)
    assert a["verdict"] == "INSUFFICIENT INFORMATION"
    assert [i["issue_type"] for i in a["quality"]["issues"]] == [issue_type]
    e = _email(s, cid)
    submitted = demo.scenario_answers(key)["RELATIVE_INVOLVEMENT_DETAILS"]
    assert e["subject"] == f"[COI Information Required] {cid} – Additional Information Required"
    for expected in ("Additional Information Required", "Q1a — Details of the relative's involvement", submitted,
                     "Why this needs attention:", "Requested clarification:", clarification_hint):
        assert expected in e["body_text"], expected
    _assert_business_only(e)


def test_contradiction_email_shows_both_answers_and_requested_clarification(demo_settings):
    s = demo_settings
    cid = _submit(s, "CONTRADICTION")
    body = _email(s, cid)["body_text"]
    assert "Inconsistent information identified" in body
    assert "Q1a — Relative's role, influence, decision-making authority or information access: No" in body
    assert demo.scenario_answers("CONTRADICTION")["RELATIVE_DESIGNATION"] in body
    assert "Requested clarification:" in body
    _assert_business_only(_email(s, cid))


def test_ai_unavailable_email_has_no_ai_internals(demo_settings):
    s = demo_settings
    db.set_setting(s, "simulate_ai_failure", "true")
    cid = _submit(s, "POTENTIAL_CONFLICT")
    e = _email(s, cid)
    assert e["subject"] == f"[COI Review Required] {cid} – Manual Review Required"
    assert "Automated Assessment Unavailable" in e["body_html"] and "Manual Review Required" in e["body_text"]
    _assert_business_only(e)


def test_info_request_email_links_open_in_new_tab(demo_settings):
    s = demo_settings
    cid = _submit(s, "GIBBERISH")
    review.record_decision(s, cid, review.REQUEST_INFO, "Rev", info_fields=["RELATIVE_INVOLVEMENT_DETAILS"],
                           info_message="Please describe your parent's role.")
    e = _email(s, cid, "INFO_REQUEST")
    assert 'target="_blank"' in e["body_html"] and "Update Declaration" in e["body_html"]
    assert not INTERNAL_IDS.search(e["body_html"]) and not INTERNAL_IDS.search(e["body_text"])


def test_preview_frame_opens_links_in_new_tab():
    from ui.components import email_preview_html
    assert email_preview_html("<a href='x'>x</a>").startswith('<base target="_blank">')


def test_other_relevant_information_uses_only_validated_mitigating_factors(demo_settings):
    s = demo_settings
    cid = _submit(s, "LEARNING_A")  # flagged by conservative guidance; respondent states no influence
    a = db.latest_assessment(s, cid)
    answers = db.latest_submission(s, cid)["answers"]
    items = other_relevant_items(a, answers, exclude={"RELATIVE_AT_SOE"})
    assert [i["qid"] for i in items] == ["RELATIVE_INFLUENCE", "RELATIVE_DESIGNATION"]
    assert all(i["value"] == answers[i["qid"]] for i in items)          # exact saved answers
    assert other_relevant_items(a, answers, exclude={"RELATIVE_INFLUENCE", "RELATIVE_DESIGNATION"}) == []
    no_factors = dict(a, result=dict(a["result"], mitigating_factors=[]))
    assert other_relevant_items(no_factors, answers, exclude=set()) == []  # nothing is invented
    assert other_relevant_items(dict(a, verdict="NO CONFLICT APPARENT"), answers, exclude=set()) == []
