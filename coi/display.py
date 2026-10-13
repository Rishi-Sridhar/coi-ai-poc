"""Business display wording shared by the Streamlit UI and the reviewer email (no Streamlit imports).

Presentation only: maps stored codes/IDs to business wording and builds display structures from persisted data.
Data-integrity rules for the Full Declaration:
- values are the exact strings persisted for the submission that was assessed (never paraphrased or corrected);
- only persisted (visible/applicable) answers are shown — hidden conditional answers were never stored;
- labels come from the questionnaire configuration; internal IDs are never shown.
"""
from __future__ import annotations

import re

from coi.questionnaire import load_questionnaire, question, questions

VERDICT_DISPLAY = {
    "POTENTIAL CONFLICT IDENTIFIED": ("Potential Conflict Identified", "danger"),
    "NO CONFLICT APPARENT": ("No Conflict Apparent", "ok"),
    "INSUFFICIENT INFORMATION": ("Additional Information Required", "warn"),
    "AI ASSESSMENT UNAVAILABLE": ("Automated Assessment Unavailable", "neutral"),
}
OUTCOME_DISPLAY = {"CONFLICT": "Conflict", "NO CONFLICT": "No Conflict"}


_ID_RE = None
# Quality-check category codes -> plain words when they appear in explanation text (stored data is unchanged).
# "INSUFFICIENT INFORMATION" (a verdict, not a category) is deliberately left alone.
_CATEGORY_WORDS = {"GIBBERISH": "as meaningless", "IRRELEVANT": "as not answering the question",
                   "INSUFFICIENT": "as incomplete", "CONTRADICTORY": "as inconsistent",
                   "INSTRUCTION_LIKE": "as containing instructions rather than an answer"}
_CATEGORY_RE = re.compile(r"\b(GIBBERISH|IRRELEVANT|INSUFFICIENT|CONTRADICTORY|INSTRUCTION_LIKE)\b(?! INFORMATION)")


def _id_re() -> re.Pattern:
    """Pattern matching any internal question ID (built once, on first use)."""
    global _ID_RE
    if _ID_RE is None:
        ids = sorted(load_questionnaire()["by_id"], key=len, reverse=True)
        _ID_RE = re.compile(r"\b(" + "|".join(map(re.escape, ids)) + r")\b")
    return _ID_RE


def humanize(text: str | None) -> str:
    """Replace internal question IDs in AI/system text with business numbering (e.g. RELATIVE_INFLUENCE -> Q1a)."""
    if not text:
        return ""
    text = _id_re().sub(lambda m: f"Q{question(m.group(1))['number']}", text)
    for code, (title, _) in VERDICT_DISPLAY.items():  # verdict codes first, e.g. NO CONFLICT APPARENT
        text = text.replace(code, title)
    text = _CATEGORY_RE.sub(lambda m: _CATEGORY_WORDS[m.group(1)], text)
    text = re.sub(r"\brisk[- ]indicator\b", "conflict-related", text)
    text = re.sub(r"\bNO CONFLICT\b", "No Conflict", text)
    text = re.sub(r"\bCONFLICT\b", "Conflict", text)
    text = re.sub(r"\bprecedents?\b", lambda m: "reference cases" if m.group(0).endswith("s") else "reference case", text)
    return re.sub(r"\bPrecedents?\b", lambda m: "Reference cases" if m.group(0).endswith("s") else "Reference case", text)


def referenced_ids(text: str | None) -> list[str]:
    return list(dict.fromkeys(_id_re().findall(text or "")))

MAX_KEY_CARDS = 3
CARD_TEXT_LIMIT = 160
CARD_FREE_TEXT_LIMIT = 90  # free-text previews in narrow cards; full text is in the Full Declaration
SUMMARY_LIMIT = 220

# Section headings in the interviewer's numbering (keyed by the major question number).
SECTION_TITLES = {
    "1": "Question 1 — Family and relatives at the State-Owned Entity",
    "2": "Question 2 — Third-party ownership or control",
    "3": "Question 3 — Prior employment or affiliation",
    "4": "Question 4 — Improperly obtained confidential information",
    "5": "Question 5 — Other conflicts",
    "6": "Question 6 — Opportunity Owner declarations",
}

# Compact overview phrases, built only from the persisted top-level Yes/No answers (no new judgement).
_OVERVIEW = [
    ("RELATIVE_AT_SOE", "Relative at SOE disclosed", "No relative at SOE"),
    ("THIRD_PARTY_INTEREST", "Third-party interest disclosed", "No third-party interest"),
    ("PRIOR_SOE_EMPLOYMENT", "Prior SOE employment disclosed", "No prior SOE employment"),
    ("IMPROPER_INFORMATION", "Improperly obtained information disclosed", "No improper information"),
    ("OTHER_CONFLICT", "Other conflict disclosed", "No other conflict"),
    ("INTERMEDIARY_INVOLVED", "Intermediary involved", "No intermediaries"),
]

_RELATIONSHIP_CONTEXT = {"RELATIVE_": "RELATIVE_RELATIONSHIP", "THIRD_PARTY_": "THIRD_PARTY_RELATIONSHIP"}


def major_number(number: str) -> str:
    m = re.match(r"\d+", number)
    return m.group(0) if m else number


def concise_summary(explanation: str, limit: int = SUMMARY_LIMIT) -> tuple[str, bool]:
    """First sentence of the existing explanation, shortened at a word boundary if needed.

    Returns (summary, shortened). Never adds content; the full explanation is shown elsewhere.
    """
    text = (explanation or "").strip()
    if not text:
        return "", False
    first = re.split(r"(?<=[.!?])\s+", text, maxsplit=1)[0]
    shortened = first != text
    if len(first) > limit:
        first = first[:limit].rsplit(" ", 1)[0].rstrip(",;:") + "…"
        shortened = True
    return first, shortened


def card_text(value: str, limit: int = CARD_TEXT_LIMIT) -> tuple[str, bool]:
    if len(value) <= limit:
        return value, False
    return value[:limit].rsplit(" ", 1)[0] + "…", True


def key_items(a: dict, answers: dict) -> tuple[str, list[dict]]:
    """Heading + ordered key response items (AI order preserved). Each: {qid, value, why, context}."""
    result = a.get("result") or {}
    verdict = a["verdict"]
    if verdict == "AI ASSESSMENT UNAVAILABLE":
        items = [{"qid": q["id"], "value": answers[q["id"]], "why": "", "context": False} for q in questions()
                 if q.get("risk_indicator") and answers.get(q["id"]) == "Yes"]
        return "Responses requiring review", items
    if verdict == "NO CONFLICT APPARENT":
        items = [{"qid": c["question_id"], "value": answers[c["question_id"]], "why": c["explanation"], "context": False}
                 for c in result.get("mitigating_factors", []) if c["question_id"] in answers]
        return "Key responses considered", items
    items = [{"qid": c["question_id"], "value": answers[c["question_id"]], "why": c["explanation"], "context": False}
             for c in result.get("contributing_factors", []) if c["question_id"] in answers]
    seen, unique = set(), []
    for it in items:  # a question may be cited twice; show it once, keeping the first (AI) position
        if it["qid"] not in seen:
            seen.add(it["qid"])
            unique.append(it)
    cited = {it["qid"] for it in unique}
    for prefix, rel in _RELATIONSHIP_CONTEXT.items():  # e.g. "Relationship: Spouse" next to a family disclosure
        if rel in answers and rel not in cited and any(q.startswith(prefix) for q in cited):
            unique.append({"qid": rel, "value": answers[rel], "why": "", "context": True})
    return "Why this was flagged", unique


def other_relevant_items(a: dict, answers: dict, exclude: set[str], limit: int = MAX_KEY_CARDS) -> list[dict]:
    """Mitigating responses for a flagged case: only validated AI mitigating factors (guardrails G3/G4 checked the
    question IDs and quoted values) with the exact saved answer. Nothing is generated here."""
    if a["verdict"] in ("NO CONFLICT APPARENT", "AI ASSESSMENT UNAVAILABLE"):
        return []  # no-conflict cases already show these as key responses; unavailable has no AI factors
    out, seen = [], set(exclude)
    for c in (a.get("result") or {}).get("mitigating_factors", []):
        qid = c["question_id"]
        if qid in answers and qid not in seen:
            seen.add(qid)
            out.append({"qid": qid, "value": answers[qid], "why": c["explanation"], "context": False})
    return out[:limit]


def split_cards(items: list[dict]) -> tuple[list[dict], list[dict]]:
    """Up to three cards; context-only items never push out a contributing response."""
    primary = [i for i in items if not i["context"]]
    context = [i for i in items if i["context"]]
    shown = primary[:MAX_KEY_CARDS]
    shown += context[: max(0, MAX_KEY_CARDS - len(shown))]
    rest = [i for i in items if i not in shown]
    return shown, rest


def overview_line(answers: dict) -> str:
    parts = [yes if answers.get(qid) == "Yes" else no for qid, yes, no in _OVERVIEW if qid in answers]
    return " · ".join(parts)


def build_declaration(answers: dict, is_owner: str, cited: set[str], issues: dict[str, dict]) -> list[dict]:
    """Sections of rows in questionnaire order, using only persisted answers.

    Row: {number, label, value, kind: yesno|text|file|derived|other, cited, issue}
    """
    sections: dict[str, dict] = {}
    for q in questions():
        major = major_number(q["number"])
        sec = sections.setdefault(major, {"title": SECTION_TITLES.get(major, f"Question {major}"), "rows": [], "note": ""})
        if q["id"] not in answers:
            continue  # not applicable / not asked for this declaration — never invented
        value = answers[q["id"]]
        kind = {"yes_no": "yesno", "textarea": "text", "text": "text", "email_list": "text", "file": "file",
                "derived": "derived"}.get(q["input_type"], "other")
        value_display = f"Uploaded — {value}" if kind == "file" else value  # file answer = stored filename
        sec["rows"].append({"qid": q["id"], "number": q["number"], "label": q["label"], "value": value_display,
                            "raw": value, "kind": kind, "cited": q["id"] in cited, "issue": issues.get(q["id"])})
    if is_owner != "Yes" and "6" in sections and not sections["6"]["rows"]:
        sections["6"]["note"] = "Not applicable — the respondent is not the Opportunity Owner."
    return [s for s in sections.values() if s["rows"] or s["note"]]


def next_action(case: dict, a: dict, decision: dict | None) -> tuple[str, str]:
    status, verdict = case["status"], a["verdict"]
    if status == "CLOSED" and decision and decision.get("final_outcome"):
        outcome = {"CONFLICT": "Conflict", "NO CONFLICT": "No Conflict"}.get(decision["final_outcome"], decision["final_outcome"])
        return "Review complete", f"Final decision recorded by the reviewer: {outcome}."
    if status == "AWAITING_INFORMATION":
        return "Awaiting respondent", ("Additional information has been requested from the respondent. "
                                       "The case will be re-assessed when they respond.")
    if status == "NOT_REVIEWED":
        return "No Manual Review Triggered", ("No conflict is apparent from the submitted information under the "
                                              "current assessment rules.")
    if verdict == "AI ASSESSMENT UNAVAILABLE":
        return "Manual Review Required", "The automated assessment could not be completed. A reviewer should assess the declaration directly."
    if verdict == "INSUFFICIENT INFORMATION":
        return "Additional Information Required", "The respondent must clarify the incomplete response before a final decision can be made."
    if verdict == "POTENTIAL CONFLICT IDENTIFIED":
        return "Compliance Review Required", "The reviewer should verify the disclosed facts and record a final decision."
    if case.get("routing_priority") == "CONFIRMATION":
        return "Compliance Confirmation Requested", "The reviewer should confirm the preliminary assessment."
    return "Compliance Review Required", ("A conflict-related question was answered “Yes”. The reviewer should "
                                          "confirm the preliminary assessment.")


def notification_label(email: dict | None) -> str:
    if not email:
        return "Not required"
    return {"SENT": "Sent", "FAILED": "Notification could not be delivered"}.get(email["delivery_status"], "Prepared")


def question_title(qid: str) -> str:
    q = question(qid)
    return f"Q{q['number']} — {q['label']}"
