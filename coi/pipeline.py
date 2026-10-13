"""Submission orchestration:

validate (deterministic) -> persist visible answers -> AI quality check -> precedent retrieval
-> AI COI assessment -> guardrails (G1–G5) -> routing floor (G6) -> outbox email.

Any AI failure (error, timeout, malformed output, guardrail failure) is retried once; if it still
fails the state is AI ASSESSMENT UNAVAILABLE and the case routes to human review. Never fails toward
NO CONFLICT.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Callable

from prompts import assessment as assessment_prompt
from prompts import quality as quality_prompt

from . import fallback
from .config import CONFIG_DIR, Settings
from .db import (audit, connect, get_case, list_precedents, next_case_id, simulate_ai_failure, utcnow)
from .emailer import (ENGINE_LABEL_FALLBACK, ENGINE_LABEL_LLM, build_reviewer_email, send_email)
from .actions import deterministic_actions, merge_actions
from .guardrails import (VERDICT_UNAVAILABLE, apply_g5, routing_floor, validate_assessment, validate_quality)
from .llm import GroqJSONClient, LLMError
from .questionnaire import question, validate_submission
from .retrieval import llm_view, retrieve

SIMULATED_FAILURE_MESSAGE = "SIMULATED AI FAILURE (Admin demo control) — fail-safe path exercised deliberately."


def load_guidance() -> str:
    return (CONFIG_DIR / "guidance.md").read_text(encoding="utf-8")


def llm_opportunity_context(context: dict) -> dict:
    """Opportunity context for the LLM. Respondent name and email are deliberately excluded."""
    return {
        "opportunity_id": context["opportunity_id"],
        "opportunity_name": context["opportunity_name"],
        "client": context["client"],
        "state_owned_entity": context["soe"],
        "soe_relationship_to_opportunity": context["soe_relationship"],
        "description": context["description"],
        "respondent_role": context["respondent_role"],
        "respondent_is_opportunity_owner": context["is_owner"],
    }


def question_defs(answers: dict) -> list[dict]:
    keys = ("id", "number", "label", "text", "input_type", "options", "risk_indicator", "required_elements")
    return [{k: question(qid)[k] for k in keys if k in question(qid)} for qid in answers]


def llm_answers(answers: dict) -> dict:
    """Answers sent to the LLM: other-individual email addresses are redacted (personal data, not needed)."""
    out = dict(answers)
    if "OTHER_BID_INDIVIDUAL_EMAILS" in out:
        out["OTHER_BID_INDIVIDUAL_EMAILS"] = f"[{len(out['OTHER_BID_INDIVIDUAL_EMAILS'].split(','))} email address(es) provided — redacted]"
    return out


@dataclass
class AIOutcome:
    state: str  # COMPLETED | AI_UNAVAILABLE
    engine: str  # LLM | DEMO_FALLBACK
    model: str
    verdict: str
    quality_issues: list[dict] = field(default_factory=list)
    quality_summary: str = ""
    result: dict | None = None
    guardrails: dict = field(default_factory=dict)
    rendered_prompt: str = ""
    raw_response: str = ""
    error: str | None = None


def _attempt_twice(call: Callable[[], tuple[dict, str]], validate: Callable[[dict], tuple[list[str], Any]],
                   log: list[dict], step: str) -> tuple[Any, str] | None:
    """Run call+validate; retry once on any failure. Returns (validated, raw) or None."""
    for attempt in (1, 2):
        try:
            data, raw = call()
        except LLMError as exc:
            log.append({"step": step, "attempt": attempt, "status": "ERROR", "detail": str(exc)})
            continue
        errors, validated = validate(data)
        if errors:
            log.append({"step": step, "attempt": attempt, "status": "GUARDRAIL_FAILED", "detail": errors, "raw": raw})
            continue
        log.append({"step": step, "attempt": attempt, "status": "OK"})
        return validated, raw
    return None


def run_ai(settings: Settings, context: dict, answers: dict, rule_results: dict, retrieved: list[dict],
           llm_client: Any = None) -> AIOutcome:
    use_fallback = settings.use_fallback_engine
    engine = "DEMO_FALLBACK" if use_fallback else "LLM"
    model = fallback.ENGINE_NAME if use_fallback else settings.groq_model
    simulate = simulate_ai_failure(settings)
    log: list[dict] = []
    ctx = llm_opportunity_context(context)
    send_answers = llm_answers(answers)
    qdefs = question_defs(answers)
    precedents_for_llm = llm_view(retrieved)
    supplied_ids = [p["precedent_id"] for p in precedents_for_llm]
    outcome = AIOutcome(state="AI_UNAVAILABLE", engine=engine, model=model, verdict=VERDICT_UNAVAILABLE)

    client_holder: dict = {}

    def client():
        if "c" not in client_holder:
            client_holder["c"] = llm_client or GroqJSONClient(settings)
        return client_holder["c"]

    def guard(fn: Callable[[], tuple[dict, str]]) -> Callable[[], tuple[dict, str]]:
        def wrapped():
            if simulate:
                raise LLMError(SIMULATED_FAILURE_MESSAGE)
            return fn()
        return wrapped

    # ---- Call 1: quality
    q_user = quality_prompt.build_user_message(ctx, qdefs, send_answers)
    prompts_seen = [f"=== QUALITY CALL — SYSTEM ===\n{quality_prompt.SYSTEM_PROMPT}\n=== QUALITY CALL — USER ===\n{q_user}"]
    if use_fallback:
        q_call = guard(lambda: (lambda d: (d, json.dumps(d, indent=2)))(fallback.assess_quality(qdefs, answers)))
    else:
        q_call = guard(lambda: client().call_json(quality_prompt.SYSTEM_PROMPT, q_user, quality_prompt.OUTPUT_SCHEMA))
    q_summary: dict = {}

    def q_validate(data):
        errs, issues = validate_quality(data, answers)
        q_summary["summary"] = data.get("summary", "") if isinstance(data, dict) else ""
        return errs, issues

    try:
        q_res = _attempt_twice(q_call, q_validate, log, "quality")
    except Exception as exc:  # noqa: BLE001 — never crash the submission
        log.append({"step": "quality", "status": "ERROR", "detail": f"{type(exc).__name__}: {exc}"})
        q_res = None
    raws = []
    if q_res is None:
        outcome.error = "Quality assessment failed after retry."
        outcome.rendered_prompt = "\n\n".join(prompts_seen)
        outcome.guardrails = {"attempts": log}
        return outcome
    outcome.quality_issues, q_raw = q_res
    outcome.quality_summary = q_summary.get("summary", "")
    raws.append(f"=== QUALITY RESPONSE ===\n{q_raw}")

    # ---- Call 2: assessment
    deterministic = {
        "validation": rule_results.get("validation"),
        "risk_indicators_answered_yes": rule_results.get("risk_indicators_yes"),
        "respondent_is_opportunity_owner": rule_results.get("is_owner"),
        "intermediary_declaration_required": rule_results.get("intermediary_declaration_required"),
        "intermediary_declaration_uploaded": rule_results.get("intermediary_declaration_uploaded"),
        "prior_employment_recency": rule_results.get("prior_employment_recency"),
        "required_actions": [{"action_code": x["action_code"], "reason": x["reason"]}
                             for x in deterministic_actions(answers, outcome.quality_issues, ai_failed=False)],
    }
    quality_for_llm = [{k: v for k, v in i.items() if k != "submitted_text_matches"} for i in outcome.quality_issues]
    a_user = assessment_prompt.build_user_message(ctx, qdefs, send_answers, deterministic, quality_for_llm,
                                                  load_guidance(), settings.guidance_version, precedents_for_llm)
    prompts_seen.append(f"=== ASSESSMENT CALL — SYSTEM ===\n{assessment_prompt.SYSTEM_PROMPT}\n=== ASSESSMENT CALL — USER ===\n{a_user}")
    if use_fallback:
        a_call = guard(lambda: (lambda d: (d, json.dumps(d, indent=2)))(
            fallback.assess_case(answers, outcome.quality_issues, precedents_for_llm,
                                  is_owner=context.get("is_owner") == "Yes")))
    else:
        a_call = guard(lambda: client().call_json(assessment_prompt.SYSTEM_PROMPT, a_user, assessment_prompt.OUTPUT_SCHEMA))
    def a_validate(data):
        return validate_assessment(data, answers, supplied_ids, outcome.quality_issues), data

    try:
        a_res = _attempt_twice(a_call, a_validate, log, "assessment")
    except Exception as exc:  # noqa: BLE001
        log.append({"step": "assessment", "status": "ERROR", "detail": f"{type(exc).__name__}: {exc}"})
        a_res = None
    outcome.rendered_prompt = "\n\n".join(prompts_seen)
    if a_res is None:
        outcome.error = "COI assessment failed after retry."
        outcome.raw_response = "\n\n".join(raws)
        outcome.guardrails = {"attempts": log}
        return outcome
    result, a_raw = a_res
    raws.append(f"=== ASSESSMENT RESPONSE ===\n{a_raw}")
    outcome.raw_response = "\n\n".join(raws)

    verdict, g5_note = apply_g5(result["verdict"], outcome.quality_issues)
    result = dict(result, ai_original_verdict=result["verdict"])
    outcome.result = result
    outcome.verdict = verdict
    outcome.state = "COMPLETED"
    outcome.guardrails = {"attempts": log, "g5": g5_note}
    return outcome


@dataclass
class SubmitResult:
    ok: bool
    errors: list[tuple[str, str]] = field(default_factory=list)
    case_id: str | None = None
    review_required: bool = False
    status: str | None = None


def _safe_filename(name: str) -> str:
    stem = re.sub(r"[^A-Za-z0-9._-]", "_", Path(name).name)
    return stem[-100:] or "upload"


def _save_upload(settings: Settings, case_id: str, upload: dict | None) -> str | None:
    if not upload:
        return None
    folder = Path(settings.uploads_dir) / case_id
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / _safe_filename(upload["name"])
    path.write_bytes(upload.get("bytes", b""))
    return str(path)


def _assess_and_route(settings: Settings, case_id: str, submission_id: int, context: dict, answers: dict,
                      rule_results: dict, llm_client: Any) -> SubmitResult:
    current = {"answers": answers, "is_owner": context["is_owner"], "soe_relationship": context["soe_relationship"]}
    retrieved = retrieve(current, list_precedents(settings, active_only=True))
    ai = run_ai(settings, context, answers, rule_results, retrieved, llm_client)

    failed = ai.state != "COMPLETED"
    guardrail_errors = [str(a["detail"]) for a in ai.guardrails.get("attempts", []) if a["status"] == "GUARDRAIL_FAILED"]
    routing = routing_floor(answers, ai.quality_issues, ai.verdict, failed, guardrail_errors)
    if ai.guardrails.get("g5"):
        routing["g5"] = ai.guardrails["g5"]
    # Recommended actions = deterministic rule actions (always) + validated AI catalogue actions.
    routing["recommended_actions"] = merge_actions(
        deterministic_actions(answers, ai.quality_issues, ai_failed=failed),
        (ai.result or {}).get("recommended_actions") or [], ai.verdict)

    if routing["review_required"]:
        status, priority = "PENDING_REVIEW", "REVIEW_REQUIRED"
    elif settings.review_clean_cases:
        status, priority = "PENDING_REVIEW", "CONFIRMATION"
    else:
        status, priority = "NOT_REVIEWED", "NONE"
    routing["priority"] = priority

    precedents_store = [{k: v for k, v in r.items() if k != "snapshot"} | {"opportunity_summary": r["snapshot"].get("opportunity_summary")}
                        for r in retrieved]
    with connect(settings) as conn:
        cur = conn.execute(
            "INSERT INTO assessments (case_id, submission_id, state, engine, quality_json, result_json, verdict, routing_json,"
            " precedents_json, guardrails_json, model, prompt_version, guidance_version, rendered_prompt, raw_response,"
            " error, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (case_id, submission_id, ai.state, ai.engine,
             json.dumps({"issues": ai.quality_issues, "summary": ai.quality_summary}),
             json.dumps(ai.result) if ai.result else None, ai.verdict, json.dumps(routing),
             json.dumps(precedents_store), json.dumps(ai.guardrails), ai.model, settings.prompt_version,
             settings.guidance_version, ai.rendered_prompt, ai.raw_response, ai.error, utcnow()),
        )
        assessment_id = cur.lastrowid
        conn.execute("UPDATE cases SET status=?, routing_priority=?, updated_at=? WHERE case_id=?",
                     (status, priority, utcnow(), case_id))

    actor = "AI" if ai.engine == "LLM" else "demo-fallback"
    if failed:
        audit(settings, case_id, "AI_FAILED", actor, {"assessment_id": assessment_id, "error": ai.error,
                                                      "attempts": ai.guardrails.get("attempts")})
    else:
        audit(settings, case_id, "QUALITY_CHECKED", actor, {"issues": [f"{i['question_id']}: {i['issue_type']}" for i in ai.quality_issues]})
        audit(settings, case_id, "ASSESSED", actor, {"assessment_id": assessment_id, "verdict": ai.verdict,
                                                     "precedents": [p["precedent_id"] for p in retrieved],
                                                     "g5": ai.guardrails.get("g5")})
    audit(settings, case_id, "ROUTED", "system", {"status": status, "priority": priority, "reasons": routing["reasons"]})

    if status == "PENDING_REVIEW":
        case = get_case(settings, case_id)
        subject, html, text = build_reviewer_email(settings, case, {
            "verdict": ai.verdict,
            "engine_label": ENGINE_LABEL_FALLBACK if ai.engine == "DEMO_FALLBACK" else ENGINE_LABEL_LLM,
            "routing": routing, "routing_priority": priority, "result": ai.result,
            "quality_issues": ai.quality_issues,
        }, answers)
        send_email(settings, case_id, settings.reviewer_email, "REVIEWER_NOTIFICATION", subject, html, text)
    return SubmitResult(ok=True, case_id=case_id, review_required=status == "PENDING_REVIEW", status=status)


def submit_declaration(settings: Settings, context: dict, raw_answers: dict, upload: dict | None = None,
                       today: date | None = None, llm_client: Any = None) -> SubmitResult:
    """upload: {"name", "size", "bytes"} for the intermediary declaration, or None."""
    validation = validate_submission(context, raw_answers, upload, today)
    if not validation.ok:
        return SubmitResult(ok=False, errors=validation.errors)

    now = utcnow()
    with connect(settings) as conn:
        case_id = next_case_id(conn)
        conn.execute(
            "INSERT INTO cases (case_id, opportunity_id, opportunity_name, client, soe, soe_relationship,"
            " opportunity_description, respondent_name, respondent_email, respondent_role, is_owner, status,"
            " routing_priority, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (case_id, context["opportunity_id"], context["opportunity_name"], context["client"], context["soe"],
             context["soe_relationship"], context["description"], context["respondent_name"],
             context["respondent_email"].strip(), context["respondent_role"], context["is_owner"], "SUBMITTED",
             None, now, now),
        )
    upload_path = _save_upload(settings, case_id, upload)
    rule_results = dict(validation.rule_results, upload_path=upload_path)
    with connect(settings) as conn:
        cur = conn.execute(
            "INSERT INTO submissions (case_id, version, answers_json, rule_results_json, questionnaire_version, created_at)"
            " VALUES (?,?,?,?,?,?)",
            (case_id, 1, json.dumps(validation.answers), json.dumps(rule_results), settings.questionnaire_version, now),
        )
        submission_id = cur.lastrowid
    audit(settings, case_id, "SUBMITTED", "respondent", {"version": 1, "answered_questions": list(validation.answers),
                                                         "hidden_answers_discarded": rule_results["hidden_answers_discarded"]})
    return _assess_and_route(settings, case_id, submission_id, context, validation.answers, rule_results, llm_client)


def resubmit_declaration(settings: Settings, case_id: str, context: dict, raw_answers: dict,
                         upload: dict | None = None, existing_upload_name: str | None = None,
                         today: date | None = None, llm_client: Any = None) -> SubmitResult:
    """Respondent response to an information request: new submission version, fresh assessment."""
    case = get_case(settings, case_id)
    if not case or case["status"] != "AWAITING_INFORMATION":
        return SubmitResult(ok=False, errors=[("case", f"{case_id} is not awaiting information.")])
    effective_upload = upload or ({"name": existing_upload_name, "size": 0} if existing_upload_name else None)
    validation = validate_submission(context, raw_answers, effective_upload, today)
    if not validation.ok:
        return SubmitResult(ok=False, errors=validation.errors)
    with connect(settings) as conn:
        version = conn.execute("SELECT MAX(version) FROM submissions WHERE case_id=?", (case_id,)).fetchone()[0] + 1
        conn.execute(
            "UPDATE cases SET opportunity_id=?, opportunity_name=?, client=?, soe=?, soe_relationship=?,"
            " opportunity_description=?, respondent_role=?, updated_at=? WHERE case_id=?",
            (context["opportunity_id"], context["opportunity_name"], context["client"], context["soe"],
             context["soe_relationship"], context["description"], context["respondent_role"], utcnow(), case_id),
        )
    upload_path = _save_upload(settings, case_id, upload) if upload else None
    rule_results = dict(validation.rule_results, upload_path=upload_path)
    with connect(settings) as conn:
        cur = conn.execute(
            "INSERT INTO submissions (case_id, version, answers_json, rule_results_json, questionnaire_version, created_at)"
            " VALUES (?,?,?,?,?,?)",
            (case_id, version, json.dumps(validation.answers), json.dumps(rule_results), settings.questionnaire_version, utcnow()),
        )
        submission_id = cur.lastrowid
    audit(settings, case_id, "RESUBMITTED", "respondent", {"version": version})
    return _assess_and_route(settings, case_id, submission_id, context, validation.answers, rule_results, llm_client)


def rerun_assessment(settings: Settings, case_id: str, llm_client: Any = None) -> SubmitResult:
    """Re-run AI on the latest submission (e.g. after an AI outage). Only for cases still pending review."""
    from .db import latest_submission
    case = get_case(settings, case_id)
    sub = latest_submission(settings, case_id)
    context = {"opportunity_id": case["opportunity_id"], "opportunity_name": case["opportunity_name"],
               "client": case["client"], "soe": case["soe"], "soe_relationship": case["soe_relationship"],
               "description": case["opportunity_description"], "respondent_role": case["respondent_role"],
               "is_owner": case["is_owner"]}
    audit(settings, case_id, "REASSESSMENT_REQUESTED", "reviewer", {"submission_version": sub["version"]})
    return _assess_and_route(settings, case_id, sub["id"], context, sub["answers"], sub["rule_results"], llm_client)
