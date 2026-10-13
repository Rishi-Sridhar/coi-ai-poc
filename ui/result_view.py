"""Executive Assessment Summary + Full Declaration Viewer (shared by the Assessment and Review pages).

Default view: verdict, one-sentence summary, up to three key responses, View Full Declaration (one click, inline),
information issues, next action, reviewer notification. Everything else is in collapsed supporting sections.
Presentation only — no business logic and no changes to stored data.
"""
from __future__ import annotations

import json
from html import escape

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

from coi.actions import action_category, action_label, display_actions, load_catalog, recommended_actions
from coi.config import Settings
from coi.db import latest_submission, one, rows
from coi.questionnaire import question
from coi.display import OUTCOME_DISPLAY, VERDICT_DISPLAY, humanize, referenced_ids
from ui.components import ACTION_DISPLAY, EVENT_LABELS, email_preview_html, engine_label, fmt_time, label
from ui.presentation import (CARD_FREE_TEXT_LIMIT, CARD_TEXT_LIMIT, MAX_KEY_CARDS, build_declaration, card_text,
                             concise_summary, key_items, next_action, notification_label, other_relevant_items,
                             overview_line,
                             question_title, split_cards)

ICONS = {"danger": "⚠︎", "ok": "✓", "warn": "ⓘ", "neutral": "–"}
_RELATION = {"APPLIED": "Followed — materially similar facts", "DISTINGUISHED": "Not followed — a material fact differs"}


# ---------------------------------------------------------------- data helpers (read-only)

def submission_for(settings: Settings, case_id: str, a: dict) -> dict:
    """The exact submission version that this assessment assessed (falls back to the latest)."""
    sub = one(settings, "SELECT * FROM submissions WHERE id=?", (a.get("submission_id"),)) if a.get("submission_id") else None
    if not sub:
        return latest_submission(settings, case_id)
    sub["answers"] = json.loads(sub["answers_json"])
    sub["rule_results"] = json.loads(sub["rule_results_json"])
    return sub


def latest_decision(settings: Settings, case_id: str) -> dict | None:
    r = rows(settings, "SELECT * FROM decisions WHERE case_id=? ORDER BY id DESC LIMIT 1", (case_id,))
    return r[0] if r else None


def reviewer_email(settings: Settings, case_id: str) -> dict | None:
    r = rows(settings, "SELECT * FROM emails WHERE case_id=? AND kind='REVIEWER_NOTIFICATION' ORDER BY id DESC LIMIT 1",
             (case_id,))
    return r[0] if r else None


def _cited(a: dict) -> set[str]:
    result = a["result"] or {}
    return {c["question_id"] for c in result.get("contributing_factors", []) + result.get("mitigating_factors", [])}


def _issues(a: dict) -> dict[str, dict]:
    return {i["question_id"]: i for i in (a["quality"] or {}).get("issues", [])}


def _qnum(qid: str) -> str:
    return f"Q{question(qid)['number']}"


# ---------------------------------------------------------------- Level 1: executive summary

def _verdict_card(a: dict) -> None:
    title, tone = VERDICT_DISPLAY.get(a["verdict"], (a["verdict"].title(), "neutral"))
    if a["verdict"] == "AI ASSESSMENT UNAVAILABLE":
        summary = "The automated assessment could not be completed. No conclusion has been drawn."
    else:
        summary, _ = concise_summary(humanize((a["result"] or {}).get("overall_explanation", "")))
    source = ("Rules-based demo assessment (Demo Mode — not AI)" if a["engine"] == "DEMO_FALLBACK"
              else "AI-assisted preliminary assessment")
    st.markdown(
        f'<div class="coi-verdict v-{tone}"><div class="k">Preliminary assessment</div>'
        f'<div class="t"><span class="ic">{ICONS[tone]}</span>{escape(title)}</div>'
        f'<div class="d">{escape(summary)}</div>'
        f'<div class="s">{escape(source)} · the final decision rests with the compliance reviewer</div></div>',
        unsafe_allow_html=True)


def _key_cards(a: dict, answers: dict, issues: dict) -> set[str]:
    heading, items = key_items(a, answers)
    label(heading)
    if not items:
        msg = ("No conflict-related question was answered “Yes”." if a["verdict"] == "AI ASSESSMENT UNAVAILABLE"
               else "No conflict indicators were identified from the submitted responses.")
        st.markdown(f'<div class="coi-text">{msg}</div>', unsafe_allow_html=True)
        return set()
    shown, rest = split_cards(items)
    cols = st.columns(MAX_KEY_CARDS)  # fixed grid: cards keep a consistent width even when fewer than three
    for col, it in zip(cols, shown):
        q = question(it["qid"])
        free_text = q["input_type"] in ("text", "textarea", "email_list")
        text, cut = card_text(it["value"], CARD_FREE_TEXT_LIMIT if free_text else CARD_TEXT_LIMIT)
        flag = '<div class="f">Needs clarification</div>' if it["qid"] in issues else ""
        more = '<div class="m">Full text in the declaration below</div>' if cut else ""
        col.markdown(f'<div class="coi-card"><div class="n">Q{escape(q["number"])}</div>'
                     f'<div class="l">{escape(q["label"])}</div><div class="v{" txt" if free_text else ""}">'
                     f'{"“" + escape(text) + "”" if free_text else escape(text)}</div>{flag}{more}</div>',
                     unsafe_allow_html=True)
    if rest:
        with st.expander(f"View all contributing responses ({len(items)})"):
            for it in items:
                st.markdown(f'<div class="coi-resp"><div class="q"><b>{escape(question_title(it["qid"]))}</b>'
                            + (f'<span class="w">{escape(humanize(it["why"]))}</span>' if it["why"] else "")
                            + f'</div><div class="a">{escape(it["value"])}</div></div>', unsafe_allow_html=True)
    return {it["qid"] for it in items}


def _other_relevant(a: dict, answers: dict, exclude: set[str]) -> None:
    """Small neutral list of mitigating responses (validated AI mitigating factors, exact saved answers)."""
    items = other_relevant_items(a, answers, exclude)
    if not items:
        return
    label("Other relevant information")
    st.markdown("".join(
        f'<div class="coi-other"><span class="q">{escape(question_title(it["qid"]))}:</span> '
        f'<b>{escape(card_text(it["value"], CARD_FREE_TEXT_LIMIT)[0])}</b>'
        + (f'<div class="w">{escape(humanize(it["why"]))}</div>' if it["why"] else "") + "</div>"
        for it in items), unsafe_allow_html=True)


def _render_issue(issue: dict, answers: dict) -> None:
    qid = issue["question_id"]
    if issue["issue_type"] == "CONTRADICTORY":
        others = [r for r in referenced_ids(issue.get("explanation")) if r != qid and r in answers]
        said = "".join(f'<div><b>{escape(question_title(r))}:</b> {escape(answers[r])}</div>' for r in others)
        body = ('<div class="h">Inconsistent information identified</div>' + said
                + f'<div class="coi-muted" style="margin-top:6px">However, the respondent also stated '
                  f'({escape(question_title(qid))}):</div>'
                + f'<div class="coi-quote">“{escape(issue["submitted_text"])}”</div>'
                + '<div>These responses appear inconsistent and require clarification before a final decision.</div>'
                + f'<div style="margin-top:4px"><b>Requested clarification:</b> '
                  f'{escape(humanize(issue["follow_up_question"]))}</div>')
    else:
        heading = ("Response cannot be relied on" if issue["issue_type"] == "INSTRUCTION_LIKE"
                   else "Response needs clarification")
        body = (f'<div class="h">{heading}</div><div><b>{escape(question_title(qid))}</b></div>'
                f'<div class="coi-muted" style="margin-top:4px">Submitted:</div>'
                f'<div class="coi-quote">“{escape(issue["submitted_text"])}”</div>'
                f'<div><b>Why this needs attention:</b> {escape(humanize(issue["explanation"]))}</div>'
                f'<div style="margin-top:4px"><b>Requested clarification:</b> {escape(humanize(issue["follow_up_question"]))}</div>')
    st.markdown(f'<div class="coi-issue">{body}</div>', unsafe_allow_html=True)


def render_full_declaration(case: dict, sub: dict, a: dict) -> None:
    """Full Declaration Viewer: the exact persisted answers of the assessed submission, inline, one click."""
    issues = _issues(a)
    sections = build_declaration(sub["answers"], case["is_owner"], _cited(a), issues)
    with st.expander("View Full Declaration", expanded=False):
        version = (f" · submission version {sub['version']} (updated after an information request)"
                   if sub["version"] > 1 else "")
        st.markdown(
            f'<div class="coi-ctx"><b>{escape(case["opportunity_id"])} — {escape(case["opportunity_name"])}</b><br>'
            f'Client: {escape(case["client"])} · SOE: {escape(case["soe"])} ({escape(case["soe_relationship"])})<br>'
            f'{escape(case["opportunity_description"] or "")}<br>'
            f'Respondent: {escape(case["respondent_name"])} · {escape(case["respondent_email"])} · '
            f'{escape(case["respondent_role"])} · Opportunity Owner: {escape(case["is_owner"])}<br>'
            f'<span class="coi-muted">Submitted {escape(fmt_time(sub["created_at"]))}{escape(version)}. Responses are shown '
            f'exactly as submitted. <span class="tag-c">Cited</span> = used in the assessment.</span></div>',
            unsafe_allow_html=True)
        overview = overview_line(sub["answers"])
        if overview:
            st.markdown(f'<div class="coi-overview"><b>Declaration overview:</b> {escape(overview)}</div>',
                        unsafe_allow_html=True)
        html = []
        for sec in sections:
            html.append(f'<div class="coi-dsec">{escape(sec["title"])}</div>')
            if sec["note"]:
                html.append(f'<div class="coi-muted">{escape(sec["note"])}</div>')
            for r in sec["rows"]:
                tags = ('<span class="tag-c">Cited</span>' if r["cited"] else "") + \
                       ('<span class="tag-i">Needs clarification</span>' if r["issue"] else "")
                if r["kind"] == "text":
                    val = f'<div class="dv txt">{escape(r["value"])}</div>'
                elif r["kind"] == "derived":
                    val = f'<div class="dv">{escape(r["value"])} <span class="coi-muted">(system-calculated)</span></div>'
                else:
                    val = f'<div class="dv">{escape(r["value"])}</div>'
                note = (f'<div class="coi-muted" style="margin-top:2px">{escape(humanize(r["issue"]["explanation"]))}</div>'
                        if r["issue"] else "")
                cls = "drow cited" if r["cited"] or r["issue"] else "drow"
                html.append(f'<div class="{cls}"><div class="dl"><b>Q{escape(r["number"])}</b> {escape(r["label"])} '
                            f'{tags}</div>{val}{note}</div>')
        st.markdown("".join(html), unsafe_allow_html=True)


def _action_card(act: dict, primary: bool) -> str:
    cat = action_category(act["action_code"])
    return (f'<div class="coi-act cat-{cat}{" primary" if primary else ""}">'
            f'<div class="t">{escape(action_label(act["action_code"]))}</div>'
            f'<div class="r">{escape(humanize(act.get("reason", "")))}</div></div>')


def render_recommended_actions(a: dict) -> None:
    """Compact Recommended Action cards (business labels only; codes are in Advanced Technical Details)."""
    actions = display_actions(recommended_actions(a))  # workflow (Compliance review) is shown as the next step
    if not actions:
        return
    label("Recommended action")
    shown, rest = actions[:3], actions[3:]
    html = _action_card(shown[0], primary=True)
    if len(shown) > 1:
        html += '<div class="coi-act-more">Additional recommendations</div>' + "".join(_action_card(x, False) for x in shown[1:])
    st.markdown(html, unsafe_allow_html=True)
    if rest:
        with st.expander(f"View all recommended actions ({len(actions)})"):
            st.markdown("".join(_action_card(x, False) for x in actions), unsafe_allow_html=True)
    st.caption("Recommended — the final action is decided by the Compliance reviewer.")


def render_assessment_summary(settings: Settings, case: dict, a: dict, sub: dict, *, reviewer: bool) -> None:
    """Level-1 executive view (+ the one-click Full Declaration)."""
    answers = sub["answers"]
    issues = _issues(a)
    _verdict_card(a)
    shown = _key_cards(a, answers, issues)
    _other_relevant(a, answers, shown)
    render_full_declaration(case, sub, a)  # the declaration overview line now sits inside this expander

    if issues:
        only_contradiction = all(i["issue_type"] == "CONTRADICTORY" for i in issues.values())
        label("Inconsistent information" if only_contradiction else "Additional information required")
        for issue in issues.values():
            _render_issue(issue, answers)

    render_recommended_actions(a)

    title, sentence = next_action(case, a, latest_decision(settings, case["case_id"]))
    label("Next action")
    st.markdown(f'<div class="coi-text"><b>{escape(title)}</b> — {escape(sentence)}</div>', unsafe_allow_html=True)

    email = reviewer_email(settings, case["case_id"])
    st.markdown(f'<div class="coi-text" style="margin-top:0.6rem">Reviewer notification: '
                f'<b>{escape(notification_label(email))}</b></div>', unsafe_allow_html=True)
    if email:
        with st.expander("View Reviewer Email"):
            st.markdown(f'<div class="coi-kv"><b>To:</b> {escape(email["recipient"])}<br>'
                        f'<b>Subject:</b> {escape(email["subject"])}</div>', unsafe_allow_html=True)
            components.html(email_preview_html(email["body_html"]), height=720, scrolling=True)
    # The structured "Recommended action" section replaces the free-text suggestion on the primary view;
    # the AI's free-text recommended_action remains under "Full Assessment Explanation".


# ---------------------------------------------------------------- Level 2 / 3: supporting (collapsed)

def render_full_explanation(a: dict, answers: dict) -> None:
    result = a["result"] or {}
    with st.expander("Full Assessment Explanation"):
        if not result:
            st.markdown("The automated assessment was unavailable, so there is no AI explanation for this case.")
            return
        st.markdown(f'<div class="coi-text">{escape(humanize(result.get("overall_explanation", "")))}</div>',
                    unsafe_allow_html=True)
        for title, key in (("Contributing factors", "contributing_factors"), ("Mitigating factors", "mitigating_factors")):
            items = result.get(key) or []
            if items:
                label(title)
                st.markdown("".join(
                    f'<div class="coi-resp"><div class="q"><b>{escape(question_title(c["question_id"]))}</b>'
                    f'<span class="w">{escape(humanize(c["explanation"]))}</span></div>'
                    f'<div class="a">{escape(answers.get(c["question_id"], ""))}</div></div>'
                    for c in items if c["question_id"] in answers), unsafe_allow_html=True)
        if result.get("recommended_action"):
            label("Recommended action")
            st.markdown(humanize(result["recommended_action"]))


def render_previous_cases(a: dict) -> None:
    retrieved = a["precedents"] or []
    analysis = {p["precedent_id"]: p for p in ((a["result"] or {}).get("precedent_analysis") or [])}
    with st.expander(f"Previous Reviewed Cases Considered ({len(retrieved)})"):
        st.caption("Earlier decided cases with similar answers were provided to the assessment as reference. "
                   "Synthetic reference cases are demonstration examples only.")
        if not retrieved:
            st.markdown("No comparable reference cases were found.")
        for p in retrieved:
            pa = analysis.get(p["precedent_id"])
            kind = "Reviewer-approved reference case" if p["source"] == "REVIEWER" else "Synthetic example"
            lines = [f'<b>Reference case {escape(p["precedent_id"])}</b> <span class="coi-muted">· {kind}</span>',
                     f'<b>Previous outcome:</b> {escape(OUTCOME_DISPLAY.get(p["outcome"], p["outcome"]))}',
                     f'<b>Reviewer rationale:</b> {escape(p["rationale"])}']
            if pa:
                lines.append(f'<b>How it was used:</b> {escape(_RELATION.get(pa["relationship_to_current_case"], ""))}')
                key = "Why it was relevant" if pa["relationship_to_current_case"] == "APPLIED" else "How this case differs"
                lines.append(f'<b>{key}:</b> {escape(humanize(pa["material_fact"]))}')
            st.markdown('<div class="coi-kv" style="padding:8px 0;border-bottom:1px solid #f2f4f7">'
                        + "<br>".join(lines) + "</div>", unsafe_allow_html=True)


def render_audit(settings: Settings, case_id: str) -> None:
    with st.expander("Audit History"):
        events = rows(settings, "SELECT * FROM audit_events WHERE case_id=? ORDER BY id", (case_id,))
        st.dataframe(pd.DataFrame([{"Time": fmt_time(e["created_at"]), "Event": EVENT_LABELS.get(e["event_type"], e["event_type"]),
                                    "By": e["actor"]} for e in events]), hide_index=True, use_container_width=True)
        decisions = rows(settings, "SELECT * FROM decisions WHERE case_id=? ORDER BY id", (case_id,))
        if decisions:
            label("Reviewer decisions")
            st.dataframe(pd.DataFrame([{
                "Time": fmt_time(d["created_at"]), "Reviewer": d["reviewer_name"],
                "Action": ACTION_DISPLAY.get(d["action"], d["action"]),
                "Final decision": OUTCOME_DISPLAY.get(d["final_outcome"], "") if d["final_outcome"] else "",
                "Rationale / comments": d["rationale"] or "", "Reference case": "Yes" if d["use_as_precedent"] else "",
                "Mitigation notes": d["mitigation_notes"] or "", "Additional comments": d["additional_comments"] or "",
            } for d in decisions]), hide_index=True, use_container_width=True)


def render_technical(case: dict, sub: dict, a: dict, email: dict | None = None) -> None:
    result = a["result"] or {}
    with st.expander("Advanced Technical Details"):
        st.markdown(f'<div class="coi-kv"><b>Assessment engine:</b> {escape(engine_label(a["engine"]))} · '
                    f'<b>Model:</b> {escape(a["model"] or "")}<br>'
                    f'<b>State:</b> {escape(a["state"])} · <b>Verdict code:</b> {escape(a["verdict"])}'
                    + (f' · <b>Model verdict before G5:</b> {escape(result["ai_original_verdict"])}'
                       if result.get("ai_original_verdict") and result["ai_original_verdict"] != a["verdict"] else "")
                    + f'<br><b>Case status:</b> {escape(case["status"])} · <b>Routing priority:</b> '
                    f'{escape(case["routing_priority"] or "")}<br>'
                    f'<b>Prompt version:</b> {escape(a["prompt_version"] or "")} · <b>Guidance version:</b> '
                    f'{escape(a["guidance_version"] or "")} · <b>Questionnaire version:</b> '
                    f'{escape(sub["questionnaire_version"] or "")} · <b>Submission:</b> v{sub["version"]} '
                    f'(id {sub["id"]})<br><b>Deep link format:</b> ?page=reviewer&amp;case={escape(case["case_id"])}'
                    + (f'<br><b>Email delivery:</b> {escape(email["delivery_status"])}'
                       + (f' — {escape(email["error"])}' if email.get("error") else "") if email else "")
                    + '</div>', unsafe_allow_html=True)
        routing = a["routing"] or {}
        label("Routing reasons (routing floor G6)")
        for r in routing.get("reasons") or ["None — clean case"]:
            st.markdown(f"- {r}")
        if routing.get("g5"):
            st.markdown(f"- {routing['g5']}")
        if a["error"]:
            label("Error")
            st.code(a["error"], language=None)
        issues = (a["quality"] or {}).get("issues", [])
        if issues:
            label("Quality issue categories")
            st.dataframe(pd.DataFrame([{"Question ID": i["question_id"], "Category": i["issue_type"],
                                        "More info required": i.get("more_info_required")} for i in issues]),
                         hide_index=True, use_container_width=True)
        label("Cited answers (verified by guardrails G3/G4)")
        cites = ([{"Role": "Contributing", **c} for c in result.get("contributing_factors", [])]
                 + [{"Role": "Mitigating", **c} for c in result.get("mitigating_factors", [])])
        st.dataframe(pd.DataFrame(cites) if cites else pd.DataFrame([{"Role": "—"}]), hide_index=True, use_container_width=True)
        label("Reference-case retrieval (weighted structured matching)")
        analysis = {p["precedent_id"]: p for p in (result.get("precedent_analysis") or [])}
        st.dataframe(pd.DataFrame([{
            "Precedent": p["precedent_id"], "Source": p["source"], "Similarity": p["score"],
            "AI relationship": analysis.get(p["precedent_id"], {}).get("relationship_to_current_case", "—"),
            "Matched answers": ", ".join(f"{m['field']}={m['value']}" for m in p["matched"]),
        } for p in (a["precedents"] or [])]), hide_index=True, use_container_width=True)
        label("Recommended actions (codes; source = rule or AI)")
        st.dataframe(pd.DataFrame([{"Action code": x["action_code"], "Source": x.get("source", "ai"),
                                    "Reason": x.get("reason", "")} for x in recommended_actions(a)] or [{"Action code": "—"}]),
                     hide_index=True, use_container_width=True)
        st.caption(load_catalog()["disclaimer"])
        label("Guardrail log")
        st.json(a["guardrails"] or {}, expanded=False)
        label("What the AI saw (rendered prompts — no respondent name or email)")
        st.code(a["rendered_prompt"] or "(no prompt — AI not called)", language=None)
        label("Raw responses")
        st.code(a["raw_response"] or "(none)", language="json")
        if result:
            label("Structured result")
            st.code(json.dumps(result, indent=2), language="json")


def render_supporting(settings: Settings, case: dict, sub: dict, a: dict, *, audit: bool = True) -> None:
    render_full_explanation(a, sub["answers"])
    render_previous_cases(a)
    if audit:
        render_audit(settings, case["case_id"])
    render_technical(case, sub, a, reviewer_email(settings, case["case_id"]))
