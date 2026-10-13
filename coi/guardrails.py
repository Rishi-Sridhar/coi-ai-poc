"""Application-side validation of AI output (G1–G5, G7 explainability, G8 actions) and the routing floor (G6)."""
from __future__ import annotations

import re

import jsonschema

from prompts import assessment as assessment_prompt
from prompts import quality as quality_prompt

from .actions import validate_ai_actions
from .questionnaire import question

VERDICT_POTENTIAL = "POTENTIAL CONFLICT IDENTIFIED"
VERDICT_NONE = "NO CONFLICT APPARENT"
VERDICT_INSUFFICIENT = "INSUFFICIENT INFORMATION"
VERDICT_UNAVAILABLE = "AI ASSESSMENT UNAVAILABLE"
ALLOWED_VERDICTS = set(assessment_prompt.VERDICTS)


def _same(a: str, b: str) -> bool:
    """Exact match after collapsing whitespace (models may re-wrap long text)."""
    collapse = lambda s: re.sub(r"\s+", " ", str(s)).strip()
    return collapse(a) == collapse(b)


def validate_quality(data: dict, answers: dict) -> tuple[list[str], list[dict]]:
    """Returns (errors, issues). Each issue's submitted_text is replaced by the exact stored answer."""
    errors: list[str] = []
    try:
        jsonschema.validate(data, quality_prompt.OUTPUT_SCHEMA)
    except jsonschema.ValidationError as exc:
        return [f"G1 quality schema invalid: {exc.message}"], []
    issues = []
    for issue in data["issues"]:
        qid = issue["question_id"]
        if qid not in answers:
            errors.append(f"G3 quality issue cites unknown question_id {qid!r}")
            continue
        issue = dict(issue)
        issue["submitted_text_matches"] = _same(issue["submitted_text"], answers[qid])
        issue["submitted_text"] = answers[qid]  # always display the stored answer, never model-quoted text
        issues.append(issue)
    return errors, issues


def validate_assessment(data: dict, answers: dict, supplied_precedent_ids: list[str],
                        quality_issues: list[dict]) -> list[str]:
    """Returns guardrail errors (empty list = valid). Any error triggers the retry / fail-safe path."""
    try:
        jsonschema.validate(data, assessment_prompt.OUTPUT_SCHEMA)
    except jsonschema.ValidationError as exc:
        return [f"G1 assessment schema invalid: {exc.message}"]
    errors: list[str] = []
    verdict = data["verdict"]
    if verdict not in ALLOWED_VERDICTS:
        errors.append(f"G2 verdict {verdict!r} is not an allowed verdict")

    # G3 / G4: every citation refers to a real answer and quotes it exactly
    for cite in data["contributing_factors"] + data["mitigating_factors"]:
        qid = cite["question_id"]
        if qid not in answers:
            errors.append(f"G3 cited question_id {qid!r} does not exist in this submission")
        elif not _same(cite["submitted_value"], answers[qid]):
            errors.append(f"G4 quoted value for {qid} ({cite['submitted_value']!r}) does not match stored answer ({answers[qid]!r})")

    # G3 (precedents): every supplied precedent analysed exactly once; nothing unknown
    analysed = [pa["precedent_id"] for pa in data["precedent_analysis"]]
    supplied = set(supplied_precedent_ids)
    unknown = sorted(set(analysed) - supplied)
    duplicates = sorted({pid for pid in analysed if analysed.count(pid) > 1})
    missing = sorted(supplied - set(analysed))
    if unknown:
        errors.append(f"G3 precedent_analysis cites precedent(s) that were not supplied: {', '.join(unknown)}")
    if duplicates:
        errors.append(f"G3 precedent_analysis lists precedent(s) more than once: {', '.join(duplicates)}")
    if missing:
        errors.append(f"G3 precedent_analysis is missing supplied precedent(s): {', '.join(missing)}")

    # Explainability consistency (not business policy): the verdict must be supported by the output itself
    if verdict == VERDICT_POTENTIAL and not data["contributing_factors"]:
        errors.append("G7 POTENTIAL CONFLICT IDENTIFIED returned without any contributing factor")
    if verdict == VERDICT_INSUFFICIENT and not (data["more_information_required"]
                                               or any(issue_requires_info(i) for i in quality_issues)):
        errors.append("G7 INSUFFICIENT INFORMATION returned without a quality issue or more_information_required=true")

    # G8 action guardrails: catalogue codes only, no duplicates, fact-based reasons, consistent combinations
    errors += validate_ai_actions(data.get("recommended_actions"), verdict, answers)
    return errors


def issue_requires_info(issue: dict) -> bool:
    return bool(issue.get("more_info_required")) or issue.get("issue_type") in quality_prompt.ISSUE_TYPES


def apply_g5(verdict: str, quality_issues: list[dict]) -> tuple[str, str | None]:
    """NO CONFLICT APPARENT cannot stand when a quality issue requires additional information."""
    if verdict == VERDICT_NONE and any(issue_requires_info(i) for i in quality_issues):
        return VERDICT_INSUFFICIENT, ("G5: AI verdict NO CONFLICT APPARENT downgraded to INSUFFICIENT INFORMATION "
                                      "because a data-quality issue requires additional information.")
    return verdict, None


def routing_floor(answers: dict, quality_issues: list[dict], verdict: str,
                  ai_failed: bool, guardrail_errors: list[str]) -> dict:
    """G6: human review is REQUIRED if any condition holds. This does not declare a conflict."""
    reasons = []
    for qid, value in answers.items():
        q = question(qid)
        if q.get("risk_indicator") and value == "Yes":
            reasons.append(f"Risk indicator answered Yes — Q{q['number']} {q['label']} ({qid})")
    for issue in quality_issues:
        reasons.append(f"AI data-quality issue — {issue['issue_type']} on {issue['question_id']}")
    if ai_failed:
        reasons.append("AI assessment unavailable — manual review required")
    elif verdict != VERDICT_NONE:
        reasons.append(f"AI preliminary verdict is {verdict}")
    for err in guardrail_errors:
        reasons.append(f"Output guardrail failed — {err}")
    return {"review_required": bool(reasons), "reasons": reasons}
