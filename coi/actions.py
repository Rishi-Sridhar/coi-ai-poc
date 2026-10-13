"""Controlled action catalogue: deterministic action rules, merging with AI recommendations, action guardrails.

The application RECOMMENDS actions; the Compliance reviewer decides the final action.
- AI recommendations are restricted to catalogue codes (schema enum + guardrail G8).
- Deterministic rules (A1–A4) are applied by code and always appear, regardless of the AI output.
"""
from __future__ import annotations

import re
from functools import lru_cache

import yaml

from .config import CONFIG_DIR
from .display import humanize, question_title, referenced_ids

NO_FURTHER_ACTION = "NO_FURTHER_ACTION"
REQUEST_INFO_ACTION = "REQUEST_ADDITIONAL_INFORMATION"
ESCALATE_COMPLIANCE = "ESCALATE_COMPLIANCE"
OBTAIN_DECLARATION = "OBTAIN_REQUIRED_DECLARATION"
HOLD_OPPORTUNITY = "HOLD_OPPORTUNITY"
# Controlled synthetic fallback when a potential conflict is supported but no more specific mitigation is justified.
HOLD_FALLBACK_REASON = ("Pause progression of the opportunity until Compliance determines the appropriate mitigation "
                        "for the disclosed conflict.")

_GENERIC_REASON = re.compile(r"^\W*(because\s+)?(a\s+|the\s+)?(potential\s+)?conflict\s+(exists|detected|identified|found)\W*$",
                             re.IGNORECASE)


@lru_cache(maxsize=1)
def load_catalog() -> dict:
    data = yaml.safe_load((CONFIG_DIR / "action_catalog.yaml").read_text(encoding="utf-8"))
    data["by_code"] = {a["code"]: a for a in data["actions"]}
    return data


def action_codes() -> list[str]:
    return [a["code"] for a in load_catalog()["actions"]]


def action_label(code: str) -> str:
    a = load_catalog()["by_code"].get(code)
    return a["label"] if a else code


def action_category(code: str) -> str:
    a = load_catalog()["by_code"].get(code)
    return a.get("category", "mitigation") if a else "mitigation"


def catalog_for_prompt() -> list[dict]:
    return [{"action_code": a["code"], "label": a["label"], "description": a["description"],
             "exclusive": bool(a.get("exclusive"))} for a in load_catalog()["actions"]]


def _act(code: str, reason: str, source: str) -> dict:
    return {"action_code": code, "reason": reason, "source": source}


# ---------------------------------------------------------------- deterministic rules (never delegated to AI)

def intermediary_declaration_missing(answers: dict) -> bool:
    return answers.get("INTERMEDIARY_INVOLVED") == "Yes" and (
        answers.get("INTERMEDIARY_DECLARATION") != "Yes" or not answers.get("INTERMEDIARY_DECLARATION_FILE"))


def deterministic_actions(answers: dict, quality_issues: list[dict], ai_failed: bool) -> list[dict]:
    """A1/A4: unusable or contradictory information -> Request Additional Information.
    A2: automated assessment unavailable -> Escalate to Compliance.
    A3: intermediary declaration not available -> Obtain Required Declaration + Hold Opportunity."""
    out: list[dict] = []
    if ai_failed:
        out.append(_act(ESCALATE_COMPLIANCE, "Manual Compliance assessment required — the automated assessment "
                                             "could not be completed.", "rule"))
    if quality_issues:
        if len(quality_issues) == 1:
            reason = humanize(quality_issues[0].get("follow_up_question") or "") or \
                f"Clarify {question_title(quality_issues[0]['question_id'])}."
        else:
            reason = "Clarify: " + "; ".join(question_title(i["question_id"]) for i in quality_issues) + "."
        out.append(_act(REQUEST_INFO_ACTION, reason, "rule"))
    if intermediary_declaration_missing(answers):
        out.append(_act(OBTAIN_DECLARATION, "An intermediary is involved (Q6.1), but the required intermediary "
                                            "Conflict of Interest declaration has not been obtained.", "rule"))
        out.append(_act(HOLD_OPPORTUNITY, "The opportunity should not progress until the intermediary declaration "
                                          "has been obtained and reviewed.", "rule"))
    return out


def merge_actions(rule_actions: list[dict], ai_actions: list[dict], verdict: str) -> list[dict]:
    """Final recommendation = deterministic + AI actions (deduplicated). Deterministic actions always appear.
    'No Further Action' is dropped whenever any other action is required."""
    ai = [_act(a["action_code"], a["reason"], a.get("source", "ai")) for a in ai_actions]
    first, second = ((rule_actions, ai) if verdict in ("INSUFFICIENT INFORMATION", "AI ASSESSMENT UNAVAILABLE")
                     else (ai, rule_actions))
    out, seen = [], set()
    for a in first + second:
        if a["action_code"] not in seen:
            seen.add(a["action_code"])
            out.append(a)
    if any(a["action_code"] != NO_FURTHER_ACTION for a in out):
        out = [a for a in out if a["action_code"] != NO_FURTHER_ACTION]
    # Completeness: a potential conflict always carries at least one business-visible recommendation.
    if verdict == "POTENTIAL CONFLICT IDENTIFIED" and not any(not is_workflow(a["action_code"]) for a in out):
        out.append(_act(HOLD_OPPORTUNITY, HOLD_FALLBACK_REASON, "rule"))
    return out


# ---------------------------------------------------------------- guardrails

def validate_ai_actions(actions: list[dict] | None, verdict: str, answers: dict) -> list[str]:
    """G8 action guardrails for the AI output. Any error -> retry once -> manual-review fail-safe."""
    if not actions:
        return ["G8 recommended_actions is missing or empty"]
    errors: list[str] = []
    codes = [a.get("action_code") for a in actions]
    known = set(action_codes())
    unknown = sorted({c for c in codes if c not in known}, key=str)
    if unknown:
        errors.append(f"G8 unknown action code(s) not in the action catalogue: {', '.join(map(str, unknown))}")
    dupes = sorted({c for c in codes if codes.count(c) > 1}, key=str)
    if dupes:
        errors.append(f"G8 duplicate action code(s): {', '.join(map(str, dupes))}")
    for a in actions:
        reason = (a.get("reason") or "").strip()
        if not reason:
            errors.append(f"G8 action {a.get('action_code')} has no reason")
        elif len(reason) < 15 or _GENERIC_REASON.match(reason):
            errors.append(f"G8 action {a.get('action_code')} reason is not tied to the submitted facts: {reason!r}")
        missing = [q for q in referenced_ids(reason) if q not in answers]
        if missing:
            errors.append(f"G8 action {a.get('action_code')} reason cites answers not in the declaration: {', '.join(missing)}")
    if NO_FURTHER_ACTION in codes and len(set(codes)) > 1:
        errors.append("G8 'No Further Action' cannot be combined with other actions")
    if verdict == "INSUFFICIENT INFORMATION" and REQUEST_INFO_ACTION not in codes:
        errors.append("G8 INSUFFICIENT INFORMATION must recommend Request Additional Information")
    if verdict == "POTENTIAL CONFLICT IDENTIFIED" and not any(c in known and not is_workflow(c) for c in codes):
        errors.append("G8 POTENTIAL CONFLICT IDENTIFIED needs at least one business-visible recommended action "
                      "(Escalate to Compliance alone is workflow, not a recommendation)")
    return errors


def is_workflow(code: str) -> bool:
    return bool(load_catalog()["by_code"].get(code, {}).get("workflow"))


def is_final_selectable(code: str) -> bool:
    return load_catalog()["by_code"].get(code, {}).get("final_action", True) is not False


def final_action_options(final_outcome: str | None) -> list[str]:
    """Actions a reviewer may record as the FINAL action for a closed decision with this outcome."""
    if final_outcome == "NO CONFLICT":
        return [NO_FURTHER_ACTION]
    if final_outcome == "CONFLICT":
        return [c for c in action_codes() if is_final_selectable(c) and c != NO_FURTHER_ACTION]
    return []


def validate_final_actions(codes: list[str] | None, final_outcome: str | None) -> list[str]:
    """Deterministic consistency between the reviewer's final COI outcome and final action(s):
    NO CONFLICT -> only No Further Action; CONFLICT -> never No Further Action; Request Additional Information
    (Request More Information workflow only) and Escalate to Compliance (workflow) are never final actions."""
    if not codes:
        return ["Select the final action(s)."]
    unknown = [c for c in codes if c not in set(action_codes())]
    if unknown:
        return [f"Unknown action(s): {', '.join(unknown)}"]
    not_final = [c for c in codes if not is_final_selectable(c)]
    if not_final:
        return [f"{', '.join(action_label(c) for c in not_final)} cannot be recorded as the final action of a "
                "Conflict / No Conflict decision (use the Request More Information workflow, or Compliance review routing)."]
    if final_outcome == "NO CONFLICT" and set(codes) != {NO_FURTHER_ACTION}:
        return ["A final decision of No Conflict only allows the final action 'No Further Action'."]
    if final_outcome == "CONFLICT" and NO_FURTHER_ACTION in codes:
        return ["A final decision of Conflict cannot have the final action 'No Further Action'."]
    if final_outcome not in ("CONFLICT", "NO CONFLICT"):
        return ["A final decision (Conflict or No Conflict) is required before recording a final action."]
    return []


def display_actions(actions: list[dict]) -> list[dict]:
    """Recommended actions for business display (result card, e-mail, reviewer 'accept'): workflow steps such as
    Escalate to Compliance are excluded — the next step already says 'Compliance Review Required'."""
    return [a for a in actions if not is_workflow(a["action_code"])]


def recommended_actions(a: dict) -> list[dict]:
    """Final recommendation stored on an assessment row (rule + AI); falls back to the AI list for older rows."""
    routing = a.get("routing") or {}
    if routing.get("recommended_actions") is not None:
        return routing["recommended_actions"]
    return [dict(x, source="ai") for x in ((a.get("result") or {}).get("recommended_actions") or [])]
