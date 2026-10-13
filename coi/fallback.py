"""DEMO MODE deterministic fallback engine.

Used ONLY when DEMO_MODE=true so the UI remains demonstrable without live API access.
Its output is always labelled "Demo fallback assessment" — it is never presented as LLM output.
It consumes the same inputs and returns the same JSON shapes as the two LLM calls, so the
application's guardrails, routing, email and review flow are exercised identically.
"""
from __future__ import annotations

import re

from .actions import HOLD_FALLBACK_REASON

ENGINE_NAME = "demo-fallback-v1"

_KEYBOARD_RUNS = ("asdf", "sdfg", "dfgh", "fghj", "ghjk", "hjkl", "qwer", "wert", "erty", "rtyu", "tyui",
                  "zxcv", "xcvb", "cvbn", "vbnm")
_JUNK = {"xyz", "abc", "qwe", "asd", "zxc", "xxx", "yyy", "zzz", "aaa", "lorem", "ipsum"}
_INSTRUCTION_RE = re.compile(
    r"(\b(ignore|disregard|forget)\b.{0,40}\b(instruction|instructions|previous|above|prompt|rules)\b)"
    r"|(\bmark\b.{0,30}\bno conflict\b)|(\bsystem prompt\b)|(\byou are (an|a) (ai|assistant|model)\b)"
    r"|(\boverride\b.{0,30}\b(verdict|assessment)\b)",
    re.IGNORECASE,
)
_DECISION_RE = re.compile(
    r"(evaluation (committee|panel)|tender (committee|board|evaluation)|bid evaluation|selecting vendors"
    r"|vendor selection|selects? (the )?vendors|decision[- ]making|approval authority|approves (the )?(tender|award|contract)"
    r"|award(s|ing)? (the )?contract|scor(e|es|ing) (the )?bids)",
    re.IGNORECASE,
)
_DOMAIN_STEMS = (
    "relativ", "spouse", "wife", "husband", "brother", "sister", "sibling", "father", "mother", "parent",
    "son", "daughter", "child", "cousin", "uncle", "aunt", "family", "role", "work", "employ", "department",
    "committee", "tender", "bid", "procur", "evaluat", "decid", "decision", "influen", "access", "informat",
    "contract", "vendor", "supplier", "opportunit", "client", "entity", "soe", "compan", "firm", "servic",
    "director", "share", "owner", "partner", "board", "manag", "officer", "engineer", "pric", "fee",
    "proposal", "competit", "colleague", "confiden", "budget", "project", "approv", "team", "consult",
    "employer", "interest", "conflict", "deal", "negotiat", "audit", "advis",
    # General service / business vocabulary, so a plain description of a service ("software licences",
    # "dredging services", "unrelated to the engagement scope") is recognised as on-topic.
    "relat", "scope", "engag", "softwar", "licen", "system", "technolog", "suppl", "install", "maint",
    "logist", "operat", "construct", "transport", "financ", "account", "legal", "market", "train",
    "manufact", "infrastruct", "deliver", "provid", "program", "strateg", "review", "assur", "implement",
)
_RELATIVE_TEXT_FIELDS = ("RELATIVE_DESIGNATION", "RELATIVE_INVOLVEMENT_DETAILS")
# Guidance rule 3 exception: passive / non-controlling interest AND services unrelated to the opportunity.
_PASSIVE_RE = re.compile(
    r"(\bno (board role|board seat|control|directorship|management|management role)\b|\bpassive\b"
    r"|\bnon-controlling\b|\bwithout (any )?control\b)", re.IGNORECASE)
_UNRELATED_RE = re.compile(
    r"(\bunrelated\b|\bnot related\b|\bno connection\b|\boutside (the|this) (scope|engagement|opportunity)\b)",
    re.IGNORECASE)

# Material facts compared for precedent applied/distinguished, in order of importance.
_MATERIAL = [
    ("RELATIVE_INFLUENCE", "relative's role, influence, decision-making authority or information access"),
    ("CONFIDENTIAL_INFORMATION", "access to confidential / price-sensitive information from prior employment"),
    ("IMPROPER_INFORMATION", "improperly obtained confidential information"),
    ("THIRD_PARTY_INTEREST", "relative's ownership/control interest in a third party"),
    ("PRIOR_EMPLOYMENT_DIRECT_INVOLVEMENT", "direct involvement in the opportunity during prior employment"),
    ("PRIOR_EMPLOYMENT_RECENCY", "recency of prior SOE employment"),
    ("RELATIVE_AT_SOE", "relative employed by the SOE"),
    ("PRIOR_SOE_EMPLOYMENT", "prior SOE employment"),
    ("OTHER_CONFLICT", "other disclosed conflict"),
    ("INTERMEDIARY_INVOLVED", "intermediary involvement"),
]
_YES_NO_MATERIAL = {k for k, _ in _MATERIAL if k != "PRIOR_EMPLOYMENT_RECENCY"}


def _tokens(text: str) -> list[str]:
    return re.findall(r"[A-Za-z0-9]+", text.lower())


def _is_real_word(tok: str) -> bool:
    if not tok.isalpha() or len(tok) < 2 or tok in _JUNK:
        return False
    if any(run in tok for run in _KEYBOARD_RUNS):
        return False
    if not re.search(r"[aeiouy]", tok) or re.search(r"[^aeiouy]{5,}", tok):
        return False
    return True


def _is_gibberish(text: str) -> bool:
    toks = _tokens(text)
    real = [t for t in toks if _is_real_word(t)]
    return len(real) < 2 or len(real) / max(len(toks), 1) < 0.5


def _is_on_topic(text: str, required_elements: list[str]) -> bool:
    """True if any word relates to the COI / service domain or to the question's required elements.

    Short stems (< 5 chars, e.g. "bid", "son") must start a word to avoid accidental matches;
    longer stems may appear anywhere in a word (e.g. "relat" in "unrelated", "licen" in "licences").
    """
    toks = _tokens(text)
    stems = _DOMAIN_STEMS + tuple(w[:5] for el in required_elements for w in _tokens(el) if len(w) > 4)
    short = tuple(st for st in stems if len(st) < 5)
    long_ = [st for st in stems if len(st) >= 5]
    return any(t.startswith(short) or any(st in t for st in long_) for t in toks)


def _follow_up(q: dict) -> str:
    elements = q.get("required_elements") or []
    ask = "; ".join(elements) if elements else "a complete answer"
    return f"Please clarify your answer to Q{q['number']} ({q['label']}), including: {ask}."


def assess_quality(question_defs: list[dict], answers: dict) -> dict:
    qdefs = {q["id"]: q for q in question_defs}
    issues = []
    for qid, value in answers.items():
        q = qdefs.get(qid)
        if not q or q["input_type"] not in ("text", "textarea") or not q.get("required_elements"):
            continue
        required = q.get("required_elements", [])
        issue_type, explanation, missing = None, "", []
        if _INSTRUCTION_RE.search(value):
            issue_type = "INSTRUCTION_LIKE"
            explanation = ("The answer contains text that attempts to instruct the assessment system rather than "
                           "answer the question. It was treated as data and cannot reduce the assessed risk.")
            missing = required
        elif _is_gibberish(value):
            issue_type = "GIBBERISH"
            explanation = "The answer consists of random characters or meaningless tokens and does not answer the question."
            missing = required
        elif (qid in _RELATIVE_TEXT_FIELDS and answers.get("RELATIVE_INFLUENCE") == "No"
              and _DECISION_RE.search(value)):
            issue_type = "CONTRADICTORY"
            explanation = ("RELATIVE_INFLUENCE was answered 'No' (no role, influence, decision-making authority or "
                           "information access), but this text describes participation in evaluation / vendor selection "
                           "or decision-making for the opportunity.")
            missing = ["a consistent statement of the relative's role and influence in relation to this opportunity"]
        elif q["input_type"] == "textarea" and not _is_on_topic(value, required):
            issue_type = "IRRELEVANT"
            explanation = "The answer is understandable but does not address what the question asks."
            missing = required
        elif (q["input_type"] == "textarea" and len(_tokens(value)) < 8) or (q["input_type"] == "text" and len(_tokens(value)) < 2):
            issue_type = "INSUFFICIENT"
            explanation = "The answer is on topic but too brief to show the required elements."
            missing = required
        if issue_type:
            issues.append({
                "question_id": qid,
                "submitted_text": value,
                "issue_type": issue_type,
                "explanation": explanation,
                "missing_elements": missing,
                "follow_up_question": _follow_up(q),
                "more_info_required": True,
            })
    summary = (f"{len(issues)} free-text quality issue(s) found." if issues else "No free-text quality issues found.")
    return {"issues": issues, "summary": summary}


def _norm(qid: str, answers: dict) -> str:
    v = answers.get(qid)
    if qid in _YES_NO_MATERIAL:
        return v if v in ("Yes", "No") else "No"  # a hidden yes/no question counts as "No"
    return v or "N/A"


def _precedent_analysis(answers: dict, precedents: list[dict], contradiction_on_relative: bool) -> list[dict]:
    current = dict(answers)
    if contradiction_on_relative:
        current["RELATIVE_INFLUENCE"] = "Yes"  # higher-risk interpretation of the contradiction
    out = []
    for p in precedents:
        prec = p.get("relevant_answers") or {}
        diffs = [(qid, label) for qid, label in _MATERIAL if _norm(qid, current) != _norm(qid, prec)]
        if not diffs:
            same = [label for qid, label in _MATERIAL if _norm(qid, current) == "Yes"] or ["no risk indicators answered Yes"]
            facts = "; ".join(f"{label} = {_norm(qid, current)}" for qid, label in _MATERIAL
                              if qid in ("RELATIVE_AT_SOE", "RELATIVE_INFLUENCE") or _norm(qid, current) == "Yes")
            out.append({
                "precedent_id": p["precedent_id"],
                "relationship_to_current_case": "APPLIED",
                "reason": (f"Materially the same facts as the current case ({', '.join(same)}). "
                           f"Precedent outcome {p['final_outcome']}: {p['rationale']}"),
                "material_fact": f"Same material facts — {facts or 'all risk indicators No'}.",
            })
        else:
            qid, label = diffs[0]
            out.append({
                "precedent_id": p["precedent_id"],
                "relationship_to_current_case": "DISTINGUISHED",
                "reason": f"A material fact differs from the current case, so the precedent's outcome ({p['final_outcome']}) does not carry over.",
                "material_fact": f"{label}: precedent = {_norm(qid, prec)}, current case = {_norm(qid, current)}.",
            })
    return out


def assess_case(answers: dict, quality_issues: list[dict], precedents: list[dict], is_owner: bool = False) -> dict:
    issues = {i["question_id"]: i for i in quality_issues}
    contradiction_on_relative = any(issues.get(f, {}).get("issue_type") == "CONTRADICTORY" for f in _RELATIVE_TEXT_FIELDS)

    def inadequate(qid: str) -> bool:
        return qid in issues and issues[qid]["issue_type"] != "CONTRADICTORY"

    strong, insufficient, mitigating, conservative = [], [], [], []
    signals: set[str] = set()  # which facts drove the result (used for action recommendations)

    def cite(qid: str, why: str) -> dict:
        return {"question_id": qid, "submitted_value": answers[qid], "explanation": why}

    def yes_with_details(flag: str, details: str, why: str) -> None:
        if answers.get(flag) != "Yes":
            return
        if details in answers and inadequate(details):
            insufficient.append(cite(flag, why + " — but the required explanation is inadequate, so the risk cannot be assessed."))
        else:
            strong.append(cite(flag, why))
            if details in answers:
                strong.append(cite(details, "Explanation provided for the disclosure above."))

    if answers.get("RELATIVE_AT_SOE") == "Yes":
        if answers.get("RELATIVE_INFLUENCE") == "Yes":
            yes_with_details("RELATIVE_INFLUENCE", "RELATIVE_INVOLVEMENT_DETAILS",
                             "Relative at the SOE has a role, influence, decision-making authority or information access relating to this opportunity")
            if not inadequate("RELATIVE_INVOLVEMENT_DETAILS"):
                signals.add("family_influence")
        elif contradiction_on_relative:
            signals.add("family_contradiction")
            strong.append(cite("RELATIVE_INFLUENCE", "Respondent states no influence, but this contradicts the free text (higher-risk interpretation applied)."))
            for f in _RELATIVE_TEXT_FIELDS:
                if issues.get(f, {}).get("issue_type") == "CONTRADICTORY":
                    strong.append(cite(f, "Free text indicates the relative participates in evaluation / decision-making for this opportunity."))
        else:
            signals.add("family_conservative")
            conservative.append(cite("RELATIVE_AT_SOE", "A relative is employed by the State-Owned Entity (conservative synthetic guidance default)."))
            mitigating.append(cite("RELATIVE_INFLUENCE", "Respondent states the relative has no role, influence, decision-making authority or information access."))
            if "RELATIVE_DESIGNATION" in answers and not inadequate("RELATIVE_DESIGNATION"):
                mitigating.append(cite("RELATIVE_DESIGNATION", "Stated role/department of the relative."))

    if answers.get("THIRD_PARTY_INTEREST") == "Yes":
        if any(inadequate(f) for f in ("THIRD_PARTY_SERVICES_NATURE", "THIRD_PARTY_OWNERSHIP")):
            insufficient.append(cite("THIRD_PARTY_INTEREST", "Relative's third-party interest disclosed, but details are inadequate."))
        elif (_PASSIVE_RE.search(answers.get("THIRD_PARTY_OWNERSHIP", ""))
              and _UNRELATED_RE.search(answers.get("THIRD_PARTY_SERVICES_NATURE", ""))):
            signals.add("third_party_passive")
            mitigating.append(cite("THIRD_PARTY_OWNERSHIP", "Passive interest with no control, board role or management position."))
            mitigating.append(cite("THIRD_PARTY_SERVICES_NATURE", "The third party's services are stated to be unrelated to the opportunity's scope."))
        else:
            signals.add("third_party_control")
            strong.append(cite("THIRD_PARTY_INTEREST", "A relative holds an ownership/control interest in a third party serving the client or SOE for this opportunity."))
            for f in ("THIRD_PARTY_SERVICES_NATURE", "THIRD_PARTY_OWNERSHIP"):
                if f in answers:
                    strong.append(cite(f, "Detail of the third-party interest."))

    if answers.get("PRIOR_SOE_EMPLOYMENT") == "Yes":
        if answers.get("PRIOR_EMPLOYMENT_RECENCY") == "<= 2 years" or answers.get("PRIOR_EMPLOYMENT_DIRECT_INVOLVEMENT") == "Yes":
            signals.add("prior_recent_or_involved")
        if answers.get("CONFIDENTIAL_INFORMATION") == "Yes" and not inadequate("CONFIDENTIAL_INFORMATION_DETAILS"):
            signals.add("confidential_info")
        if answers.get("PRIOR_EMPLOYMENT_RECENCY") == "<= 2 years":
            strong.append(cite("PRIOR_EMPLOYMENT_RECENCY", "Prior SOE employment ended within the last 2 years."))
        if answers.get("PRIOR_EMPLOYMENT_DIRECT_INVOLVEMENT") == "Yes":
            strong.append(cite("PRIOR_EMPLOYMENT_DIRECT_INVOLVEMENT", "Respondent was directly involved in this opportunity."))
        yes_with_details("CONFIDENTIAL_INFORMATION", "CONFIDENTIAL_INFORMATION_DETAILS",
                         "Prior employment gave access to confidential / price-sensitive information or decision influence")
        if (answers.get("PRIOR_EMPLOYMENT_RECENCY") == "> 2 years" and answers.get("PRIOR_EMPLOYMENT_DIRECT_INVOLVEMENT") == "No"
                and answers.get("CONFIDENTIAL_INFORMATION") == "No"):
            mitigating.append(cite("PRIOR_EMPLOYMENT_RECENCY", "Prior employment ended more than 2 years ago, with no involvement or relevant access."))

    yes_with_details("IMPROPER_INFORMATION", "IMPROPER_INFORMATION_DETAILS", "Respondent holds improperly obtained confidential information")
    yes_with_details("OTHER_CONFLICT", "OTHER_CONFLICT_DETAILS", "Respondent discloses another conflict of interest")
    if answers.get("IMPROPER_INFORMATION") == "Yes" and not inadequate("IMPROPER_INFORMATION_DETAILS"):
        signals.add("improper_info")
    if answers.get("OTHER_CONFLICT") == "Yes" and not inadequate("OTHER_CONFLICT_DETAILS"):
        signals.add("other_conflict")

    if answers.get("INTERMEDIARY_INVOLVED") == "Yes":
        if answers.get("INTERMEDIARY_DECLARATION") == "No":
            signals.add("intermediary_no_declaration")
            strong.append(cite("INTERMEDIARY_DECLARATION", "Intermediary involved but the Conflict of Interest declaration has not been obtained."))
        else:
            mitigating.append(cite("INTERMEDIARY_INVOLVED", "Intermediary involved; declaration obtained and uploaded."))

    cited = {c["question_id"] for c in strong + insufficient}
    for qid, issue in issues.items():
        if qid not in cited and issue["issue_type"] != "CONTRADICTORY" and qid in answers:
            insufficient.append(cite(qid, f"Answer flagged {issue['issue_type']} by the quality check."))

    analysis = _precedent_analysis(answers, precedents, contradiction_on_relative)
    by_id = {p["precedent_id"]: p for p in precedents}
    applied_no_conflict = [a for a in analysis if a["relationship_to_current_case"] == "APPLIED"
                           and by_id[a["precedent_id"]]["final_outcome"] == "NO CONFLICT"]

    if strong:
        verdict = "POTENTIAL CONFLICT IDENTIFIED"
        contributing = strong + insufficient
        explanation = "One or more answers indicate a potential conflict of interest under the synthetic guidance."
        if contradiction_on_relative:
            explanation += " A contradiction between structured and free-text answers was assessed using the higher-risk interpretation."
        if insufficient:
            explanation += " Some answers also require more information."
        action = "Reviewer to assess the cited facts and decide CONFLICT / NO CONFLICT, or request more information."
    elif insufficient:
        verdict = "INSUFFICIENT INFORMATION"
        contributing = insufficient
        explanation = ("A disclosure requires an explanation, but the explanation provided is inadequate, so the nature of "
                       "the risk cannot be assessed. No other adequately explained answer independently indicates a conflict.")
        action = "Reviewer should request more information from the respondent using the suggested follow-up questions."
    elif conservative and applied_no_conflict:
        verdict = "NO CONFLICT APPARENT"
        contributing = []
        prec_ids = ", ".join(a["precedent_id"] for a in applied_no_conflict)
        explanation = (f"A relative is employed by the SOE, but the stated facts (no role, influence or relevant access) are "
                       f"materially the same as reviewer-approved precedent {prec_ids}, which concluded NO CONFLICT. "
                       f"The precedent was applied in preference to the default conservative guidance.")
        mitigating = mitigating + conservative
        action = "Reviewer to confirm the preliminary assessment."
    elif conservative:
        verdict = "POTENTIAL CONFLICT IDENTIFIED"
        contributing = conservative
        explanation = ("A relative is employed by the State-Owned Entity. Under the conservative synthetic guidance this is "
                       "treated as a potential conflict because the absence of influence cannot be independently verified, "
                       "and no applicable reviewer-approved precedent was found.")
        action = "Reviewer to verify the relative's role; override to NO CONFLICT if the relative has no involvement, influence or access."
    else:
        verdict = "NO CONFLICT APPARENT"
        contributing = []
        explanation = "All risk-indicator answers are No and no free-text answer indicates a conflict."
        action = "No action required unless the reviewer has other information."

    return {
        "verdict": verdict,
        "recommended_actions": _recommend_actions(verdict, answers, quality_issues, signals, analysis, by_id, is_owner),
        "overall_explanation": explanation,
        "contributing_factors": contributing,
        "mitigating_factors": mitigating,
        "recommended_action": action,
        "quality_data_issues": [f"{i['question_id']}: {i['issue_type']}" for i in quality_issues],
        "more_information_required": bool(insufficient) or bool(quality_issues),
        "precedent_analysis": analysis,
    }


def _short(text: str, limit: int = 110) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[:limit].rsplit(" ", 1)[0] + "…"


def _recommend_actions(verdict: str, answers: dict, quality_issues: list[dict], signals: set[str],
                       analysis: list[dict], by_id: dict, is_owner: bool = False) -> list[dict]:
    """Catalogue actions with reasons tied to the submitted facts (demo-mode equivalent of the AI's selection).

    Precedence: a materially matching reviewer-approved reference case's FINAL (human) action, then the
    synthetic action guidance below. Deterministic rule actions are added later by the application.
    Escalate to Compliance is workflow (Compliance review), not a mitigation: it is only used internally when no
    specific mitigation is justified. Recusal is recommended only when the facts give the respondent a
    decision-making role on this opportunity (Opportunity Owner) — never by inventing such a role."""
    actions: list[dict] = []

    def add(code: str, reason: str) -> None:
        if code not in {a["action_code"] for a in actions}:
            actions.append({"action_code": code, "reason": reason})

    rel = (answers.get("RELATIVE_RELATIONSHIP") or "relative").lower()
    applied_reviewer = [by_id[a["precedent_id"]] for a in analysis
                        if a["relationship_to_current_case"] == "APPLIED" and by_id[a["precedent_id"]]["source"] == "REVIEWER"
                        and by_id[a["precedent_id"]].get("final_actions")]

    if verdict == "INSUFFICIENT INFORMATION" or quality_issues:
        follow = quality_issues[0].get("follow_up_question") if quality_issues else ""
        add("REQUEST_ADDITIONAL_INFORMATION", follow or "Clarify the incomplete response before a final decision is made.")
    if applied_reviewer and verdict != "INSUFFICIENT INFORMATION":
        p = applied_reviewer[0]
        for code in p["final_actions"]:
            add(code, f"Follows reviewer-approved reference case {p['precedent_id']}, which has materially the same facts "
                      f"(final decision: {p['final_outcome'].title()}).")
    elif verdict != "INSUFFICIENT INFORMATION":
        details = _short(answers.get("RELATIVE_INVOLVEMENT_DETAILS", ""))
        if "family_influence" in signals:
            add("DISENGAGE_INDIVIDUAL", f"Remove the respondent from this opportunity because their {rel} is involved in it: “{details}”")
        if "family_contradiction" in signals:
            add("REQUEST_ADDITIONAL_INFORMATION", f"Clarify whether the {rel} takes part in evaluation or decision-making: "
                                                  f"Q1a says No, but Q1b states “{_short(answers.get('RELATIVE_DESIGNATION', ''))}”.")
        if "family_conservative" in signals:
            add("REQUEST_ADDITIONAL_INFORMATION", f"Confirm that the {rel} ({_short(answers.get('RELATIVE_DESIGNATION', ''), 80)}) "
                                                  "has no involvement, influence or access relevant to this opportunity.")
        if "third_party_control" in signals:
            name = answers.get("THIRD_PARTY_NAME", "the third party")
            owner_rel = (answers.get("THIRD_PARTY_RELATIONSHIP") or "relative").lower()
            holding = _short(answers.get("THIRD_PARTY_OWNERSHIP", ""), 60)
            if is_owner:
                add("RECUSE_FROM_DECISION_MAKING", f"As Opportunity Owner, the respondent should not take part in decisions "
                                                   f"affecting {name}, in which their {owner_rel} holds {holding}.")
            else:
                add("DISENGAGE_INDIVIDUAL", f"Remove the respondent from this opportunity because their {owner_rel} holds "
                                            f"{holding} in {name}, which serves the "
                                            f"{(answers.get('THIRD_PARTY_SERVICES_TO') or 'client').lower()} in connection with it.")
        if "confidential_info" in signals:
            add("ESTABLISH_INFORMATION_BARRIER", "Restrict the respondent's access to opportunity information because prior SOE "
                                                 "employment gave them confidential information: "
                                                 f"“{_short(answers.get('CONFIDENTIAL_INFORMATION_DETAILS', ''))}”")
        if "prior_recent_or_involved" in signals:
            why = ("left the SOE within the last 2 years" if answers.get("PRIOR_EMPLOYMENT_RECENCY") == "<= 2 years"
                   else "was directly involved in this opportunity at the SOE")
            add("DISENGAGE_INDIVIDUAL", f"Remove the respondent from this opportunity: they {why}.")
        if "improper_info" in signals:
            info = _short(answers.get("IMPROPER_INFORMATION_DETAILS", ""))
            add("RESTRICT_INFORMATION_USE", f"Ensure the information described is not used, retained or shared: “{info}”")
            add("ESTABLISH_INFORMATION_BARRIER", "Separate the respondent from pricing and bid-strategy work on this opportunity "
                                                 "because they hold improperly obtained information.")
            add("ESCALATE_LEGAL_COMPLIANCE", "Improperly obtained competitor or confidential information raises potential "
                                             "competition and legal concerns that need specialist review.")
        if "other_conflict" in signals:
            # No more specific mitigation can be derived deterministically from free-text "other conflict" details.
            add("HOLD_OPPORTUNITY", HOLD_FALLBACK_REASON)
        if "intermediary_no_declaration" in signals:
            add("OBTAIN_REQUIRED_DECLARATION", "An intermediary is involved (Q6.1), but the required intermediary Conflict of "
                                               "Interest declaration has not been obtained.")
            add("HOLD_OPPORTUNITY", "The opportunity should not progress until the intermediary declaration has been "
                                    "obtained and reviewed.")
    if not actions:
        if "third_party_passive" in signals:
            reason = ("No additional mitigation is recommended: the relative's interest is passive "
                      f"(“{_short(answers.get('THIRD_PARTY_OWNERSHIP', ''), 80)}”) and the services are unrelated to this opportunity.")
        elif verdict == "NO CONFLICT APPARENT":
            reason = "No additional mitigation is recommended based on the information provided, subject to Compliance confirmation."
        else:
            reason = HOLD_FALLBACK_REASON
        add("NO_FURTHER_ACTION" if verdict == "NO CONFLICT APPARENT" else "HOLD_OPPORTUNITY", reason)
    if any(a["action_code"] != "NO_FURTHER_ACTION" for a in actions):
        actions = [a for a in actions if a["action_code"] != "NO_FURTHER_ACTION"]
    return actions
