"""Reviewer actions: CONFIRM / OVERRIDE / REQUEST MORE INFORMATION, the reviewer-owned FINAL ACTION, and
reviewer-approved precedents (which record the human final decision AND final action)."""
from __future__ import annotations

import json

from .actions import REQUEST_INFO_ACTION, validate_final_actions
from .config import Settings
from .db import (audit, connect, get_case, latest_assessment, latest_submission, next_reviewer_precedent_id, utcnow)
from .emailer import build_info_request_email, send_email
from .guardrails import VERDICT_NONE, VERDICT_POTENTIAL

CONFIRM, OVERRIDE, REQUEST_INFO = "CONFIRM", "OVERRIDE", "REQUEST_MORE_INFORMATION"
OUTCOMES = ("CONFLICT", "NO CONFLICT")
CONFIRMABLE = {VERDICT_POTENTIAL: "CONFLICT", VERDICT_NONE: "NO CONFLICT"}


class DecisionError(ValueError):
    pass


def _blank(s: str | None) -> bool:
    return not (s or "").strip()


def record_decision(settings: Settings, case_id: str, action: str, reviewer_name: str,
                    final_outcome: str | None = None, rationale: str = "", info_fields: list[str] | None = None,
                    info_message: str = "", use_as_precedent: bool = False,
                    mitigation_notes: str = "", additional_comments: str = "",
                    final_actions: list[str] | None = None, final_action_notes: str = "") -> dict:
    """final_actions: catalogue codes chosen by the reviewer (required for Confirm/Override; the reviewer is never
    assumed to agree with the recommended actions). Request More Information records Request Additional Information."""
    case = get_case(settings, case_id)
    if not case:
        raise DecisionError(f"Case {case_id} not found.")
    if case["status"] != "PENDING_REVIEW":
        raise DecisionError(f"Case {case_id} is {case['status']}; only PENDING_REVIEW cases can be decided.")
    if _blank(reviewer_name):
        raise DecisionError("Enter the reviewer name in the sidebar.")
    assessment = latest_assessment(settings, case_id)
    ai_verdict = assessment["verdict"]

    info_request = None
    if action == CONFIRM:
        if ai_verdict not in CONFIRMABLE:
            raise DecisionError(f"AI verdict {ai_verdict} cannot be confirmed; choose OVERRIDE with an outcome, or request information.")
        final_outcome = CONFIRMABLE[ai_verdict]
        agrees = True
    elif action == OVERRIDE:
        if final_outcome not in OUTCOMES:
            raise DecisionError("Override requires selecting CONFLICT or NO CONFLICT.")
        if _blank(rationale):
            raise DecisionError("Override rationale is mandatory.")
        agrees = CONFIRMABLE.get(ai_verdict) == final_outcome if ai_verdict in CONFIRMABLE else None
    elif action == REQUEST_INFO:
        if not info_fields:
            raise DecisionError("Select the affected field(s).")
        if _blank(info_message):
            raise DecisionError("Enter a message to the respondent.")
        if use_as_precedent:
            raise DecisionError("A request for more information cannot be used as a precedent.")
        final_outcome, agrees = None, None
        info_request = {"fields": info_fields, "message": info_message.strip()}
    else:
        raise DecisionError(f"Unknown action {action}.")

    if use_as_precedent and _blank(rationale):
        raise DecisionError("Rationale is mandatory when the decision is used as a precedent.")

    if action == REQUEST_INFO:
        final_actions = [REQUEST_INFO_ACTION]  # no substantive final action until the respondent replies
    else:
        problems = validate_final_actions(final_actions, final_outcome)  # checked BEFORE anything is persisted
        if problems:
            raise DecisionError(problems[0])
        final_actions = list(dict.fromkeys(final_actions))

    new_status = "AWAITING_INFORMATION" if action == REQUEST_INFO else "CLOSED"
    with connect(settings) as conn:
        cur = conn.execute(
            "INSERT INTO decisions (case_id, assessment_id, action, final_outcome, agrees_with_ai, rationale,"
            " info_request_json, use_as_precedent, mitigation_notes, additional_comments, reviewer_name, created_at,"
            " final_action_codes, final_action_notes) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (case_id, assessment["id"], action, final_outcome, None if agrees is None else int(agrees),
             rationale.strip(), json.dumps(info_request) if info_request else None, int(use_as_precedent),
             mitigation_notes.strip(), additional_comments.strip(), reviewer_name.strip(), utcnow(),
             json.dumps(final_actions), final_action_notes.strip()),
        )
        decision_id = cur.lastrowid
        conn.execute("UPDATE cases SET status=?, updated_at=? WHERE case_id=?", (new_status, utcnow(), case_id))

    audit(settings, case_id, "DECISION_RECORDED", reviewer_name, {
        "decision_id": decision_id, "action": action, "ai_verdict": ai_verdict, "final_outcome": final_outcome,
        "agrees_with_ai": agrees, "rationale": rationale.strip(), "status": new_status,
        "final_actions": final_actions, "final_action_notes": final_action_notes.strip()})

    result = {"decision_id": decision_id, "status": new_status, "final_outcome": final_outcome, "precedent_id": None,
              "final_actions": final_actions}
    if action == REQUEST_INFO:
        audit(settings, case_id, "INFO_REQUESTED", reviewer_name, info_request | {"status": "AWAITING_INFORMATION"})
        sub = latest_submission(settings, case_id)
        subject, html, text = build_info_request_email(settings, case, info_fields, sub["answers"], info_message, reviewer_name)
        send_email(settings, case_id, case["respondent_email"], "INFO_REQUEST", subject, html, text)
    if use_as_precedent:
        result["precedent_id"] = create_reviewer_precedent(settings, case_id, decision_id, final_outcome, rationale,
                                                           reviewer_name, final_actions, final_action_notes)
    return result


def create_reviewer_precedent(settings: Settings, case_id: str, decision_id: int, outcome: str,
                              rationale: str, reviewer_name: str, final_actions: list[str] | None = None,
                              final_action_notes: str = "") -> str:
    """Anonymised snapshot: no respondent name/email, no other-individual emails, no client/SOE names.
    Includes the HUMAN final decision and final action(s), which take precedence over the original AI recommendation."""
    case = get_case(settings, case_id)
    sub = latest_submission(settings, case_id)
    answers = {k: v for k, v in sub["answers"].items() if k != "OTHER_BID_INDIVIDUAL_EMAILS"}
    snapshot = {
        "title": f"Reviewer decision on {case_id}",
        "opportunity_summary": (f"{case['opportunity_description'].strip()} SOE relationship {case['soe_relationship']}. "
                                f"Respondent role {case['respondent_role']}; "
                                f"{'opportunity owner' if case['is_owner'] == 'Yes' else 'not opportunity owner'}."),
        "is_owner": case["is_owner"],
        "soe_relationship": case["soe_relationship"],
        "answers": answers,
        "ai_verdict_at_decision": latest_assessment(settings, case_id)["verdict"],
        "final_actions": list(final_actions or []),
        "final_action_notes": (final_action_notes or "").strip(),
    }
    with connect(settings) as conn:
        pid = next_reviewer_precedent_id(conn)
        conn.execute(
            "INSERT INTO precedents (id, source, case_id, decision_id, snapshot_json, outcome, rationale,"
            " guidance_version, active, created_at) VALUES (?,?,?,?,?,?,?,?,1,?)",
            (pid, "REVIEWER", case_id, decision_id, json.dumps(snapshot), outcome, rationale.strip(),
             settings.guidance_version, utcnow()),
        )
    audit(settings, case_id, "PRECEDENT_ADDED", reviewer_name, {"precedent_id": pid, "outcome": outcome,
                                                                "final_actions": list(final_actions or [])})
    return pid


def set_precedent_active(settings: Settings, precedent_id: str, active: bool, actor: str, reason: str = "") -> None:
    with connect(settings) as conn:
        row = conn.execute("SELECT case_id FROM precedents WHERE id=?", (precedent_id,)).fetchone()
        if active:
            conn.execute("UPDATE precedents SET active=1, retired_at=NULL, retired_reason=NULL WHERE id=?", (precedent_id,))
        else:
            conn.execute("UPDATE precedents SET active=0, retired_at=?, retired_reason=? WHERE id=?",
                         (utcnow(), reason.strip() or "Retired by reviewer", precedent_id))
    audit(settings, row["case_id"] if row else None, "PRECEDENT_REACTIVATED" if active else "PRECEDENT_RETIRED", actor,
          {"precedent_id": precedent_id, "reason": reason})
