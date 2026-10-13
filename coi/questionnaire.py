"""Questionnaire config, conditional visibility and deterministic validation.

Deterministic validation is intentionally MINIMAL: presence/format/business rules only.
It never judges meaning — semantically poor text (e.g. "asdfgh xyz 123") passes here and is
assessed by the AI quality assessor instead.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from .config import CONFIG_DIR

INTERMEDIARY_BLOCK_MESSAGE = (
    "Submission cannot be completed until the Intermediary Conflict of Interest declaration is uploaded."
)
INTERMEDIARY_INCONSISTENT_MESSAGE = (
    "Your answer to Q6.2b says the intermediary Conflict of Interest declaration has not been obtained, but a "
    "declaration document has been uploaded. These are inconsistent. If the uploaded document is the required "
    "intermediary declaration, please change your answer to Q6.2b to “Yes”."
)
ALLOWED_UPLOAD_EXTENSIONS = {".pdf", ".docx", ".jpg", ".jpeg", ".png"}
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+'-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
SOE_RELATIONSHIPS = ["Client", "Procuring / deciding entity", "Counterparty", "Other"]

CONTEXT_FIELDS = {
    "opportunity_id": "Opportunity ID",
    "opportunity_name": "Opportunity Name",
    "client": "Client",
    "soe": "State-Owned Entity",
    "soe_relationship": "State-Owned Entity relationship to opportunity",
    "description": "Short opportunity / service description",
    "respondent_name": "Respondent Name",
    "respondent_email": "Respondent Email",
    "respondent_role": "Respondent Role on this opportunity",
    "is_owner": "Is Opportunity Owner?",
}
TEXT_TYPES = {"text", "textarea", "email_list"}


@lru_cache(maxsize=1)
def load_questionnaire(path: str | None = None) -> dict:
    data = yaml.safe_load(Path(path or CONFIG_DIR / "questionnaire.yaml").read_text(encoding="utf-8"))
    data["by_id"] = {q["id"]: q for q in data["questions"]}
    return data


def questions() -> list[dict]:
    return load_questionnaire()["questions"]


def question(qid: str) -> dict:
    return load_questionnaire()["by_id"][qid]


def risk_indicator_ids() -> list[str]:
    return [q["id"] for q in questions() if q.get("risk_indicator")]


def _conditions(show_if: Any) -> list[dict]:
    if not show_if:
        return []
    return show_if if isinstance(show_if, list) else [show_if]


def _blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and value.strip() == "")


def has_meaningful_chars(text: str) -> bool:
    """Rejects blank, whitespace-only and punctuation-only text. Does NOT judge meaning."""
    return bool(re.search(r"[A-Za-z0-9]", text or ""))


def compute_recency(last_date: date | str, today: date) -> str:
    if isinstance(last_date, str):
        last_date = date.fromisoformat(last_date)
    try:
        cutoff = today.replace(year=today.year - 2)
    except ValueError:  # 29 Feb
        cutoff = today.replace(year=today.year - 2, day=28)
    return "<= 2 years" if last_date >= cutoff else "> 2 years"


def to_str(value: Any) -> str | None:
    if _blank(value):
        return None
    if isinstance(value, date):
        return value.isoformat()
    return str(value).strip()


def multiselect_items(value: Any) -> list[str]:
    """Accepts a widget list or a stored/demo string ("Spouse, Parent" or "Sibling")."""
    if value is None:
        return []
    items = value.split(",") if isinstance(value, str) else [str(v) for v in value]
    return [v.strip() for v in items if v and v.strip()]


def answer_to_str(q: dict, value: Any) -> str | None:
    """Canonical stored form of an answer. Multiselect: comma-separated, de-duplicated, in option order."""
    if q["input_type"] != "multiselect":
        return to_str(value)
    items = list(dict.fromkeys(multiselect_items(value)))
    if not items:
        return None
    order = {opt: i for i, opt in enumerate(q.get("options", []))}
    return ", ".join(sorted(items, key=lambda v: (order.get(v, len(order)), v)))


def is_visible(q: dict, visible_so_far: dict[str, str], is_owner: bool) -> bool:
    if q.get("applies_to", "all") == "owner" and not is_owner:
        return False
    return all(visible_so_far.get(c["question"]) == c["equals"] for c in _conditions(q.get("show_if")))


def visible_question_ids(raw_answers: dict[str, Any], is_owner: bool, today: date | None = None) -> list[str]:
    return [q["id"] for q in questions() if q["id"] in _visible_state(raw_answers, is_owner, today)[0]]


def _visible_state(raw_answers: dict[str, Any], is_owner: bool, today: date | None) -> tuple[set[str], dict[str, str]]:
    """Walk questions in order; a question is visible only if its parents are visible and match.

    Returns (visible ids, visible answered values as strings). Values for hidden questions are
    never returned — this is what prevents stale conditional answers from being persisted.
    """
    today = today or date.today()
    visible: set[str] = set()
    values: dict[str, str] = {}
    for q in questions():
        if not is_visible(q, values, is_owner):
            continue
        visible.add(q["id"])
        if q["input_type"] == "derived":
            if q["id"] == "PRIOR_EMPLOYMENT_RECENCY" and values.get("PRIOR_EMPLOYMENT_LAST_DATE"):
                values[q["id"]] = compute_recency(values["PRIOR_EMPLOYMENT_LAST_DATE"], today)
            continue
        val = answer_to_str(q, raw_answers.get(q["id"]))
        if val is not None:
            values[q["id"]] = val
    return visible, values


def visible_answers(raw_answers: dict[str, Any], is_owner: bool, today: date | None = None) -> dict[str, str]:
    return _visible_state(raw_answers, is_owner, today)[1]


def parse_email_list(text: str) -> list[str]:
    return [e.strip() for e in re.split(r"[,;\s]+", text or "") if e.strip()]


@dataclass
class ValidationResult:
    errors: list[tuple[str, str]] = field(default_factory=list)
    answers: dict[str, str] = field(default_factory=dict)
    rule_results: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.errors


def validate_submission(
    context: dict[str, Any],
    raw_answers: dict[str, Any],
    upload: dict | None = None,
    today: date | None = None,
) -> ValidationResult:
    """Deterministic validation. `upload` = {"name": str, "size": int} for the intermediary file, or None."""
    today = today or date.today()
    result = ValidationResult()
    err = result.errors

    # 1. Opportunity / context fields
    for key, label in CONTEXT_FIELDS.items():
        if _blank(context.get(key)):
            err.append((key, f"{label} is required."))
        elif key not in ("is_owner", "soe_relationship") and not has_meaningful_chars(str(context[key])):
            err.append((key, f"{label} must contain letters or numbers."))
    email = (context.get("respondent_email") or "").strip()
    if email and not EMAIL_RE.match(email):
        err.append(("respondent_email", "Respondent Email is not a valid email address."))
    if context.get("soe_relationship") and context["soe_relationship"] not in SOE_RELATIONSHIPS:
        err.append(("soe_relationship", "Select a valid State-Owned Entity relationship."))

    is_owner = context.get("is_owner") == "Yes"
    raw = dict(raw_answers)
    if upload:
        raw["INTERMEDIARY_DECLARATION_FILE"] = upload["name"]
    else:
        raw.pop("INTERMEDIARY_DECLARATION_FILE", None)

    visible_ids, answers = _visible_state(raw, is_owner, today)

    # 2. Every visible required question answered; free text must not be blank / punctuation-only
    for q in questions():
        qid = q["id"]
        if qid not in visible_ids or q["input_type"] in ("derived", "file"):
            continue
        val = answers.get(qid)
        label = f"Q{q['number']} {q['label']}"
        if q.get("required_when_visible") and val is None:
            err.append((qid, f"{label}: an answer is required."))
            continue
        if val is not None and q["input_type"] in TEXT_TYPES and not has_meaningful_chars(val):
            err.append((qid, f"{label}: enter a meaningful answer (not only punctuation or spaces)."))
        if val is not None and q["input_type"] == "yes_no" and val not in ("Yes", "No"):
            err.append((qid, f"{label}: answer must be Yes or No."))
        if val is not None and q["input_type"] == "select" and val not in q.get("options", []):
            err.append((qid, f"{label}: select a valid option."))
        if val is not None and q["input_type"] == "multiselect":
            invalid = [v for v in multiselect_items(val) if v not in q.get("options", [])]
            if invalid:
                err.append((qid, f"{label}: invalid option(s): {', '.join(invalid)}"))
        if val is not None and q["input_type"] == "date":
            try:
                if date.fromisoformat(val) > today:
                    err.append((qid, f"{label}: date cannot be in the future."))
            except ValueError:
                err.append((qid, f"{label}: invalid date."))
        if val is not None and q["input_type"] == "email_list":
            emails = parse_email_list(val)
            bad = [e for e in emails if not EMAIL_RE.match(e)]
            if not emails:
                err.append((qid, f"{label}: enter at least one email address."))
            elif bad:
                err.append((qid, f"{label}: invalid email address(es): {', '.join(bad)}"))
            else:
                answers[qid] = ", ".join(emails)

    # 3. EXPLICIT BUSINESS RULE: intermediary involved => declaration upload mandatory (never delegated to AI)
    intermediary = answers.get("INTERMEDIARY_INVOLVED") == "Yes"
    if intermediary and not upload:
        err.append(("INTERMEDIARY_DECLARATION_FILE", INTERMEDIARY_BLOCK_MESSAGE))
    if intermediary and upload and answers.get("INTERMEDIARY_DECLARATION") == "No":
        # Consistency rule: "declaration not obtained" contradicts an uploaded declaration document.
        err.append(("INTERMEDIARY_DECLARATION", INTERMEDIARY_INCONSISTENT_MESSAGE))
    if intermediary and upload:
        ext = Path(upload["name"]).suffix.lower()
        if ext not in ALLOWED_UPLOAD_EXTENSIONS:
            err.append(("INTERMEDIARY_DECLARATION_FILE", "Declaration must be a PDF, DOCX, JPG or PNG file."))
        if upload.get("size", 0) > MAX_UPLOAD_BYTES:
            err.append(("INTERMEDIARY_DECLARATION_FILE", "Declaration file exceeds the 10 MB limit."))

    supplied = {k for k, v in raw.items() if not _blank(v)}
    result.answers = answers
    result.rule_results = {
        "validation": "PASSED" if not err else "FAILED",
        "is_owner": "Yes" if is_owner else "No",
        "owner_only_questions_applied": is_owner,
        "risk_indicators_yes": [qid for qid in risk_indicator_ids() if answers.get(qid) == "Yes"],
        "intermediary_declaration_required": intermediary,
        "intermediary_declaration_uploaded": bool(upload) if intermediary else None,
        "prior_employment_recency": answers.get("PRIOR_EMPLOYMENT_RECENCY"),
        "hidden_answers_discarded": sorted(supplied - set(answers)),
        "validated_on": today.isoformat(),
    }
    return result
