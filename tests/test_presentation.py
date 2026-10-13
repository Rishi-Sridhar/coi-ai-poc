"""Presentation logic: executive summary helpers and Full Declaration fidelity (exact persisted answers)."""
from coi import db, demo, pipeline, review
from conftest import ALL_NO
from ui.presentation import (build_declaration, card_text, concise_summary, key_items, next_action,
                             notification_label, overview_line, split_cards)
from ui.result_view import submission_for


def _rows(sections):
    return [r for s in sections for r in s["rows"]]


def test_concise_summary_first_sentence_and_safe_truncation():
    assert concise_summary("One. Two. Three.") == ("One.", True)
    assert concise_summary("Only sentence.") == ("Only sentence.", False)
    long = "word " * 80 + "end."
    text, cut = concise_summary(long)
    assert cut and text.endswith("…") and len(text) <= 221 and long.startswith(text[:-1].strip())
    assert concise_summary("") == ("", False)


def test_card_text_truncates_display_only():
    assert card_text("short") == ("short", False)
    text, cut = card_text("x " * 200)
    assert cut and text.endswith("…")


def test_declaration_preserves_exact_text_and_shows_only_persisted_answers():
    answers = dict(ALL_NO, RELATIVE_AT_SOE="Yes", RELATIVE_INFLUENCE="Yes",
                   RELATIVE_INVOLVEMENT_DETAILS="asdfgh xyz 123 qwerty\n  second line , teh typo",
                   RELATIVE_RELATIONSHIP="Spouse, Parent", RELATIVE_DESIGNATION="Engineer, Grid")
    issue = {"question_id": "RELATIVE_INVOLVEMENT_DETAILS", "issue_type": "GIBBERISH", "explanation": "meaningless"}
    sections = build_declaration(answers, "No", {"RELATIVE_INFLUENCE"}, {"RELATIVE_INVOLVEMENT_DETAILS": issue})
    rows = {r["qid"]: r for r in _rows(sections)}
    assert set(rows) == set(answers)                      # nothing missing, nothing invented
    for qid, value in answers.items():
        assert rows[qid]["value"] == value                # exact, uncorrected text (incl. typo, newline, spacing)
    assert rows["RELATIVE_RELATIONSHIP"]["value"] == "Spouse, Parent"
    assert rows["RELATIVE_INFLUENCE"]["cited"] and rows["RELATIVE_INVOLVEMENT_DETAILS"]["issue"] is issue
    titles = [s["title"] for s in sections]
    assert titles[0].startswith("Question 1") and titles[-1].startswith("Question 6")
    assert sections[-1]["note"].startswith("Not applicable") and not sections[-1]["rows"]
    assert [r["number"] for r in sections[0]["rows"]] == ["1", "1a", "1a", "1b", "1b"]


def test_declaration_owner_file_and_derived_rows():
    answers = dict(ALL_NO, PRIOR_SOE_EMPLOYMENT="Yes", PRIOR_EMPLOYMENT_ENTITY="Eastvale", PRIOR_EMPLOYMENT_ROLE="Analyst, Finance",
                   PRIOR_EMPLOYMENT_LAST_DATE="2020-01-31", PRIOR_EMPLOYMENT_RECENCY="> 2 years",
                   PRIOR_EMPLOYMENT_DIRECT_INVOLVEMENT="No", CONFIDENTIAL_INFORMATION="No",
                   INTERMEDIARY_INVOLVED="Yes", INTERMEDIARY_TYPE="Lobbyist", INTERMEDIARY_CLIENT_NOMINATED="No",
                   INTERMEDIARY_DECLARATION="Yes", INTERMEDIARY_DECLARATION_FILE="decl.pdf", OTHER_BID_INDIVIDUALS="No")
    rows = {r["qid"]: r for r in _rows(build_declaration(answers, "Yes", set(), {}))}
    assert rows["INTERMEDIARY_DECLARATION_FILE"]["value"] == "Uploaded — decl.pdf"
    assert rows["PRIOR_EMPLOYMENT_RECENCY"]["kind"] == "derived" and rows["PRIOR_EMPLOYMENT_RECENCY"]["value"] == "> 2 years"
    assert rows["INTERMEDIARY_INVOLVED"]["value"] == "Yes"


def test_key_items_keep_ai_order_dedupe_and_cap_at_three():
    a = {"verdict": "POTENTIAL CONFLICT IDENTIFIED", "result": {"contributing_factors": [
        {"question_id": "OTHER_CONFLICT", "explanation": "x"}, {"question_id": "RELATIVE_INFLUENCE", "explanation": "y"},
        {"question_id": "OTHER_CONFLICT", "explanation": "dup"}, {"question_id": "RELATIVE_AT_SOE", "explanation": "z"},
        {"question_id": "THIRD_PARTY_INTEREST", "explanation": "w"}]}}
    answers = dict(ALL_NO, OTHER_CONFLICT="Yes", RELATIVE_AT_SOE="Yes", RELATIVE_INFLUENCE="Yes", THIRD_PARTY_INTEREST="Yes",
                   RELATIVE_RELATIONSHIP="Spouse")
    heading, items = key_items(a, answers)
    assert heading == "Why this was flagged"
    assert [i["qid"] for i in items] == ["OTHER_CONFLICT", "RELATIVE_INFLUENCE", "RELATIVE_AT_SOE", "THIRD_PARTY_INTEREST",
                                         "RELATIVE_RELATIONSHIP"]
    shown, rest = split_cards(items)
    assert [i["qid"] for i in shown] == ["OTHER_CONFLICT", "RELATIVE_INFLUENCE", "RELATIVE_AT_SOE"]
    assert len(rest) == 2


def test_key_items_clean_case_is_empty_and_relationship_fills_spare_card():
    clean = {"verdict": "NO CONFLICT APPARENT", "result": {"mitigating_factors": [], "contributing_factors": []}}
    assert key_items(clean, ALL_NO)[1] == []
    a = {"verdict": "POTENTIAL CONFLICT IDENTIFIED", "result": {"contributing_factors": [
        {"question_id": "RELATIVE_INFLUENCE", "explanation": "y"}]}}
    shown, rest = split_cards(key_items(a, dict(ALL_NO, RELATIVE_AT_SOE="Yes", RELATIVE_INFLUENCE="Yes",
                                                 RELATIVE_RELATIONSHIP="Sibling"))[1])
    assert [i["qid"] for i in shown] == ["RELATIVE_INFLUENCE", "RELATIVE_RELATIONSHIP"] and not rest


def test_overview_line_uses_only_structured_answers():
    assert overview_line(ALL_NO) == ("No relative at SOE · No third-party interest · No prior SOE employment · "
                                     "No improper information · No other conflict")
    assert overview_line(dict(ALL_NO, RELATIVE_AT_SOE="Yes")).startswith("Relative at SOE disclosed")


def test_next_action_and_notification_wording():
    a = {"verdict": "POTENTIAL CONFLICT IDENTIFIED"}
    assert next_action({"status": "PENDING_REVIEW"}, a, None)[0] == "Compliance Review Required"
    assert next_action({"status": "NOT_REVIEWED"}, {"verdict": "NO CONFLICT APPARENT"}, None)[0] == "No Manual Review Triggered"
    assert next_action({"status": "PENDING_REVIEW"}, {"verdict": "INSUFFICIENT INFORMATION"}, None)[0] == "Additional Information Required"
    assert next_action({"status": "PENDING_REVIEW"}, {"verdict": "AI ASSESSMENT UNAVAILABLE"}, None)[0] == "Manual Review Required"
    assert "Conflict" in next_action({"status": "CLOSED"}, a, {"final_outcome": "CONFLICT"})[1]
    assert [notification_label(e) for e in (None, {"delivery_status": "SENT"}, {"delivery_status": "NOT_CONFIGURED"},
                                            {"delivery_status": "FAILED"})] == \
        ["Not required", "Sent", "Prepared", "Notification could not be delivered"]


def test_full_declaration_matches_persisted_submission_end_to_end(demo_settings):
    s = demo_settings
    r = pipeline.submit_declaration(s, demo.scenario_context("GIBBERISH"), demo.scenario_answers("GIBBERISH"))
    a = db.latest_assessment(s, r.case_id)
    sub = submission_for(s, r.case_id, a)
    rows = {x["qid"]: x for x in _rows(build_declaration(sub["answers"], "No", set(), {}))}
    assert {k: v["value"] for k, v in rows.items()} == sub["answers"]
    assert rows["RELATIVE_INVOLVEMENT_DETAILS"]["value"] == "asdfgh xyz 123 qwerty"


def test_viewer_uses_the_submission_version_that_was_assessed(demo_settings):
    s = demo_settings
    cid = pipeline.submit_declaration(s, demo.scenario_context("GIBBERISH"), demo.scenario_answers("GIBBERISH")).case_id
    first = db.latest_assessment(s, cid)
    review.record_decision(s, cid, review.REQUEST_INFO, "Rev", info_fields=["RELATIVE_INVOLVEMENT_DETAILS"], info_message="Explain")
    new = dict(demo.scenario_answers("GIBBERISH"), RELATIVE_INVOLVEMENT_DETAILS="My father chairs the tender evaluation committee.")
    pipeline.resubmit_declaration(s, cid, demo.scenario_context("GIBBERISH"), new)
    latest = db.latest_assessment(s, cid)
    assert submission_for(s, cid, latest)["version"] == 2
    assert submission_for(s, cid, latest)["answers"]["RELATIVE_INVOLVEMENT_DETAILS"].startswith("My father")
    assert submission_for(s, cid, first)["version"] == 1
    assert submission_for(s, cid, first)["answers"]["RELATIVE_INVOLVEMENT_DETAILS"] == "asdfgh xyz 123 qwerty"
