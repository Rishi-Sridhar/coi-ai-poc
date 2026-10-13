"""Deterministic validation, conditional visibility, owner-only questions, intermediary upload rule."""
from datetime import date

from coi.questionnaire import (INTERMEDIARY_BLOCK_MESSAGE, compute_recency, validate_submission, visible_answers)
from conftest import ALL_NO, CONTEXT

OWNER = dict(CONTEXT, is_owner="Yes")


def _ids(result):
    return {f for f, _ in result.errors}


def test_all_no_non_owner_passes():
    r = validate_submission(CONTEXT, ALL_NO)
    assert r.ok, r.errors
    assert r.answers == ALL_NO


def test_conditional_required_when_parent_yes():
    r = validate_submission(CONTEXT, dict(ALL_NO, RELATIVE_AT_SOE="Yes"))
    assert {"RELATIVE_INFLUENCE", "RELATIVE_RELATIONSHIP", "RELATIVE_DESIGNATION"} <= _ids(r)
    assert "RELATIVE_INVOLVEMENT_DETAILS" not in _ids(r)  # only visible once Q1a = Yes
    r = validate_submission(CONTEXT, dict(ALL_NO, RELATIVE_AT_SOE="Yes", RELATIVE_INFLUENCE="Yes",
                                          RELATIVE_RELATIONSHIP="Spouse", RELATIVE_DESIGNATION="Engineer, Grid"))
    assert _ids(r) == {"RELATIVE_INVOLVEMENT_DETAILS"}


def test_blank_whitespace_punctuation_rejected_but_gibberish_allowed():
    base = dict(ALL_NO, RELATIVE_AT_SOE="Yes", RELATIVE_INFLUENCE="Yes", RELATIVE_RELATIONSHIP="Parent",
                RELATIVE_DESIGNATION="Deputy GM, Metering")
    for bad in ["", "   ", "?!... ---"]:
        assert "RELATIVE_INVOLVEMENT_DETAILS" in _ids(validate_submission(CONTEXT, dict(base, RELATIVE_INVOLVEMENT_DETAILS=bad)))
    r = validate_submission(CONTEXT, dict(base, RELATIVE_INVOLVEMENT_DETAILS="asdfgh xyz 123 qwerty"))
    assert r.ok, "semantic gibberish must reach the AI quality assessor, not be blocked deterministically"
    assert r.answers["RELATIVE_INVOLVEMENT_DETAILS"] == "asdfgh xyz 123 qwerty"


def test_context_and_email_validation():
    r = validate_submission(dict(CONTEXT, respondent_email="not-an-email", client="  "), ALL_NO)
    assert {"respondent_email", "client"} <= _ids(r)


def test_owner_only_questions_ignored_for_non_owner():
    answers = dict(ALL_NO, INTERMEDIARY_INVOLVED="Yes", OTHER_BID_INDIVIDUALS="Yes")
    r = validate_submission(CONTEXT, answers)
    assert r.ok
    assert "INTERMEDIARY_INVOLVED" not in r.answers and "OTHER_BID_INDIVIDUALS" not in r.answers
    assert "INTERMEDIARY_INVOLVED" in r.rule_results["hidden_answers_discarded"]


def test_owner_questions_required_for_owner():
    r = validate_submission(OWNER, ALL_NO)
    assert {"INTERMEDIARY_INVOLVED", "OTHER_BID_INDIVIDUALS"} <= _ids(r)


def test_intermediary_upload_blocks_submission():
    answers = dict(ALL_NO, INTERMEDIARY_INVOLVED="Yes", INTERMEDIARY_TYPE="Lobbyist", INTERMEDIARY_CLIENT_NOMINATED="No",
                   INTERMEDIARY_DECLARATION="Yes", OTHER_BID_INDIVIDUALS="No")
    r = validate_submission(OWNER, answers)
    assert ("INTERMEDIARY_DECLARATION_FILE", INTERMEDIARY_BLOCK_MESSAGE) in r.errors
    ok = validate_submission(OWNER, answers, upload={"name": "decl.pdf", "size": 1000})
    assert ok.ok, ok.errors
    assert ok.answers["INTERMEDIARY_DECLARATION_FILE"] == "decl.pdf"
    assert not validate_submission(OWNER, answers, upload={"name": "decl.exe", "size": 10}).ok
    assert not validate_submission(OWNER, answers, upload={"name": "decl.pdf", "size": 11 * 1024 * 1024}).ok


def test_other_bid_individual_emails_validated():
    answers = dict(ALL_NO, INTERMEDIARY_INVOLVED="No", OTHER_BID_INDIVIDUALS="Yes")
    assert "OTHER_BID_INDIVIDUAL_EMAILS" in _ids(validate_submission(OWNER, dict(answers, OTHER_BID_INDIVIDUAL_EMAILS="a@b.com, nope")))
    r = validate_submission(OWNER, dict(answers, OTHER_BID_INDIVIDUAL_EMAILS="a@b.com\nc@d.org"))
    assert r.ok and r.answers["OTHER_BID_INDIVIDUAL_EMAILS"] == "a@b.com, c@d.org"


def test_stale_hidden_answers_not_returned():
    """User answers Yes + children, then flips the parent to No: children must not be persisted."""
    raw = dict(ALL_NO, RELATIVE_AT_SOE="No", RELATIVE_INFLUENCE="Yes", RELATIVE_INVOLVEMENT_DETAILS="old text",
               RELATIVE_RELATIONSHIP="Spouse", RELATIVE_DESIGNATION="old")
    assert visible_answers(raw, is_owner=False) == ALL_NO
    # Grandchild hidden when the middle answer flips to No
    raw2 = dict(raw, RELATIVE_AT_SOE="Yes", RELATIVE_INFLUENCE="No")
    va = visible_answers(raw2, is_owner=False)
    assert "RELATIVE_INVOLVEMENT_DETAILS" not in va and va["RELATIVE_INFLUENCE"] == "No"


def test_prior_employment_recency_calculated():
    today = date(2026, 10, 5)
    assert compute_recency("2025-01-01", today) == "<= 2 years"
    assert compute_recency("2024-10-05", today) == "<= 2 years"
    assert compute_recency("2024-10-04", today) == "> 2 years"
    raw = dict(ALL_NO, PRIOR_SOE_EMPLOYMENT="Yes", PRIOR_EMPLOYMENT_ENTITY="Eastvale", PRIOR_EMPLOYMENT_ROLE="Analyst, Finance",
               PRIOR_EMPLOYMENT_LAST_DATE=date(2020, 1, 31), PRIOR_EMPLOYMENT_DIRECT_INVOLVEMENT="No",
               CONFIDENTIAL_INFORMATION="No")
    r = validate_submission(CONTEXT, raw, today=today)
    assert r.ok, r.errors
    assert r.answers["PRIOR_EMPLOYMENT_RECENCY"] == "> 2 years"
    assert "PRIOR_SOE_EMPLOYMENT" in r.rule_results["risk_indicators_yes"]
