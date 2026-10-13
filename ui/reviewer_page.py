"""REVIEW page: deep link ?page=reviewer&case=COI-0007, or pick from cases awaiting review.

Layout (desktop): case header; LEFT = preliminary verdict, why it was flagged, View Full Declaration, information
issues, next action, notification; RIGHT = reviewer decision. Supporting detail is collapsed below.
Streamlit stacks the two columns on narrow screens.
"""
from __future__ import annotations

import json
from html import escape

import streamlit as st

from coi.actions import (REQUEST_INFO_ACTION, action_label, display_actions, final_action_options, load_catalog,
                         recommended_actions, validate_final_actions)
from coi.config import Settings
from coi.db import get_case, latest_assessment, rows
from coi.guardrails import VERDICT_NONE, VERDICT_POTENTIAL
from coi.pipeline import rerun_assessment
from coi.review import CONFIRM, OVERRIDE, REQUEST_INFO, DecisionError, record_decision
from coi.display import OUTCOME_DISPLAY, humanize
from ui.components import ACTION_DISPLAY, STATUS_DISPLAY, page_header, pill, qlabel, section, set_query
from ui.result_view import (latest_decision, render_assessment_summary, render_supporting, submission_for)

REFERENCE_HELP = "Future assessments may consider this reviewed decision when evaluating similar circumstances."
ACTION_LABELS = {CONFIRM: "Confirm Assessment", OVERRIDE: "Override Assessment", REQUEST_INFO: "Request More Information"}


def _on_case_pick() -> None:
    case = (st.session_state.get("case_pick") or "").split(" · ")[0]
    if case:
        st.session_state["selected_case"] = case
        set_query(page="reviewer", case=case)


def _case_picker(settings: Settings) -> str | None:
    pending = rows(settings, "SELECT case_id, opportunity_name FROM cases WHERE status='PENDING_REVIEW' ORDER BY case_id")
    selected = st.session_state.get("selected_case")
    options = [p["case_id"] for p in pending]
    labels = {p["case_id"]: f"{p['case_id']} · {p['opportunity_name']}" for p in pending}
    if selected and selected not in options:
        case = get_case(settings, selected)
        if case:
            options.insert(0, selected)
            labels[selected] = f"{selected} · {case['opportunity_name']} · {STATUS_DISPLAY.get(case['status'], '')}"
    if selected in options:
        st.session_state["case_pick"] = labels[selected]
    st.selectbox(f"Cases awaiting review ({len(pending)})", [labels[o] for o in options], index=None, key="case_pick",
                 on_change=_on_case_pick, placeholder="Select a case…")
    if selected and not get_case(settings, selected):
        st.error(f"Case {selected} was not found.")
        return None
    return selected


def _render_final_action(a: dict, k: str, action: str, final_outcome: str | None) -> tuple[list[str] | None, str]:
    """Reviewer-owned FINAL ACTION. The reviewer must explicitly accept or modify the recommended actions.
    Options are consistent with the final decision (No Conflict -> No Further Action only; Conflict -> no
    'No Further Action'); Request Additional Information and Escalate to Compliance are never final actions."""
    st.markdown("**Final action**")
    if action == REQUEST_INFO:
        st.caption(f"Final action recorded: {action_label(REQUEST_INFO_ACTION)}. No substantive action is recorded "
                   "until the respondent provides the information.")
        return None, ""
    if final_outcome is None:
        st.caption("Select the final decision first.")
        return None, ""
    allowed = final_action_options(final_outcome)
    recommended = [x["action_code"] for x in display_actions(recommended_actions(a))]
    acceptable = [c for c in recommended if c in allowed]
    can_accept = bool(acceptable) and acceptable == recommended and not validate_final_actions(acceptable, final_outcome)
    if recommended:
        st.markdown("".join(f"<div class='coi-kv'>☑ {escape(action_label(c))}</div>" for c in recommended),
                    unsafe_allow_html=True)
    if not can_accept:
        st.caption("The recommended actions do not fit this final decision — select the final action(s).")
    modes = (["Accept recommended actions"] if can_accept else []) + ["Modify actions"]
    mode = st.radio("Final action", modes, index=None, key=f"{k}_final_mode_{final_outcome}",
                    label_visibility="collapsed")  # keyed by decision: resets cleanly if the decision changes
    chosen: list[str] | None = None
    if mode == "Accept recommended actions":
        chosen = acceptable
    elif mode == "Modify actions":
        labels = {action_label(c): c for c in allowed}
        default = [action_label(c) for c in acceptable] or ([action_label(allowed[0])] if len(allowed) == 1 else [])
        picked = st.multiselect("Select final action(s)", list(labels), key=f"{k}_final_actions_{final_outcome}",
                                default=default)
        chosen = [labels[x] for x in picked]
    else:
        st.caption("Accept the recommended actions or modify them.")
    notes = st.text_area("Additional action notes (optional)", key=f"{k}_final_notes", height=68)
    with st.expander("Available Compliance actions"):
        st.caption(load_catalog()["disclaimer"])
        st.markdown("".join(f"<div class='coi-kv' style='margin:4px 0'><b>{escape(x['label'])}</b> — "
                            f"{escape(x['description'])}</div>" for x in load_catalog()["actions"]
                            if x.get("final_action", True) is not False), unsafe_allow_html=True)
    return chosen, notes


def _render_decision(settings: Settings, case: dict, a: dict, answers: dict) -> None:
    section("Reviewer decision")
    if case["status"] != "PENDING_REVIEW":
        d = latest_decision(settings, case["case_id"])
        if d:
            outcome = OUTCOME_DISPLAY.get(d["final_outcome"], "") if d["final_outcome"] else ""
            st.markdown(f'<div class="coi-kv"><b>{escape(ACTION_DISPLAY.get(d["action"], d["action"]))}</b>'
                        + (f'<br>Final decision: <b>{escape(outcome)}</b>' if outcome else "")
                        + (f'<br>Final action: <b>{escape(", ".join(action_label(c) for c in json.loads(d["final_action_codes"])))}</b>'
                           if d.get("final_action_codes") else "")
                        + f'<br>By {escape(d["reviewer_name"] or "")} · {escape(d["created_at"][:10])}'
                        + (f'<br>{escape(d["rationale"])}' if d["rationale"] else "") + "</div>",
                        unsafe_allow_html=True)
        else:
            st.markdown(f"No decision is pending — {STATUS_DISPLAY.get(case['status'], case['status']).lower()}.")
        return

    verdict = a["verdict"]
    confirmable = verdict in (VERDICT_POTENTIAL, VERDICT_NONE)
    actions = [CONFIRM, OVERRIDE, REQUEST_INFO] if confirmable else [OVERRIDE, REQUEST_INFO]
    k = f"dec_{case['case_id']}"
    by_label = {ACTION_LABELS[x]: x for x in actions}
    # No pre-selection: the reviewer must actively choose an action.
    picked_action = st.radio("Action", list(by_label), index=None, key=f"{k}_action")
    if not confirmable:
        st.caption("There is no conclusive preliminary verdict to confirm — record the final decision via "
                   "Override, or request more information.")
    if picked_action is None:
        st.caption("Choose an action to continue.")
        return
    action = by_label[picked_action]

    outcome, fields, message = None, [], ""
    if action == CONFIRM:
        st.markdown(f"Final decision: **{'Conflict' if verdict == VERDICT_POTENTIAL else 'No Conflict'}**")
    if action == OVERRIDE:
        picked = st.radio("Final decision", ["Conflict", "No Conflict"], index=None, horizontal=True, key=f"{k}_outcome")
        outcome = {"Conflict": "CONFLICT", "No Conflict": "NO CONFLICT"}.get(picked)
    if action == REQUEST_INFO:
        field_labels = {qlabel(q): q for q in answers}
        flagged = [i["question_id"] for i in (a["quality"] or {}).get("issues", []) if i["question_id"] in answers]
        picked = st.multiselect("Questions to clarify", list(field_labels), key=f"{k}_fields",
                                default=[qlabel(q) for q in flagged])
        fields = [field_labels[x] for x in picked]
        default_msg = " ".join(humanize(i["follow_up_question"]) for i in (a["quality"] or {}).get("issues", []))
        message = st.text_area("Clarification message to the respondent", value=default_msg, key=f"{k}_message")

    final_outcome = (("CONFLICT" if verdict == VERDICT_POTENTIAL else "NO CONFLICT") if action == CONFIRM
                     else outcome if action == OVERRIDE else None)
    final_actions, final_action_notes = _render_final_action(a, k, action, final_outcome)

    rationale_label = {CONFIRM: "Reviewer comments", OVERRIDE: "Reason for override (required)",
                       REQUEST_INFO: "Internal note (optional)"}[action]
    rationale = st.text_area(rationale_label, key=f"{k}_rationale", height=90)
    use_prec = False
    if action != REQUEST_INFO:
        use_prec = st.checkbox("Use this reviewed decision as a future reference case", value=False,
                               key=f"{k}_prec", help=REFERENCE_HELP)
        st.caption(REFERENCE_HELP + (" Reviewer comments are required." if action == CONFIRM else ""))
    with st.expander("Additional notes (optional)"):
        mitigation = st.text_area("Mitigation notes", key=f"{k}_mitigation", height=70)
        comments = st.text_area("Additional comments", key=f"{k}_comments", height=70)
    reviewer_name = st.text_input("Reviewer name", value=st.session_state.get("reviewer_name", ""), key=f"{k}_reviewer")
    if st.button("Save decision", type="primary", key=f"{k}_submit", use_container_width=True):
        st.session_state["reviewer_name"] = reviewer_name
        try:
            res = record_decision(settings, case["case_id"], action, reviewer_name,
                                  final_outcome=outcome, rationale=rationale, info_fields=fields, info_message=message,
                                  use_as_precedent=use_prec, mitigation_notes=mitigation, additional_comments=comments,
                                  final_actions=final_actions, final_action_notes=final_action_notes)
        except DecisionError as exc:
            st.error(str(exc))
            return
        if action == REQUEST_INFO:
            msg = "Information requested — the respondent has been sent a clarification request."
        else:
            msg = (f"Decision saved — final decision: **{OUTCOME_DISPLAY[res['final_outcome']]}**; final action: "
                   f"**{', '.join(action_label(c) for c in res['final_actions'])}**.")
        if res["precedent_id"]:
            msg += f" Saved as reference case **{res['precedent_id']}** for future assessments."
        st.session_state["_decision_flash"] = msg
        st.rerun()


def render(settings: Settings) -> None:
    page_header("Review", "Review the preliminary assessment and record the final compliance decision.", settings.demo_mode)
    flash = st.session_state.pop("_decision_flash", None)
    if flash:
        st.success(flash)
    case_id = _case_picker(settings)
    if not case_id:
        st.info("Select a case awaiting review, or open the review link from a reviewer email.")
        return
    case = get_case(settings, case_id)
    a = latest_assessment(settings, case_id)
    sub = submission_for(settings, case_id, a)

    st.markdown(f'<h2 style="margin:0.8rem 0 0.2rem 0">Case {escape(case_id)} &nbsp;'
                f'{pill(STATUS_DISPLAY.get(case["status"], case["status"]))}</h2>'
                f'<div class="coi-kv"><b>{escape(case["opportunity_name"])}</b> · {escape(case["client"])} · '
                f'SOE: {escape(case["soe"])}<br>Respondent: {escape(case["respondent_name"])} — '
                f'{escape(case["respondent_role"])}{" (Opportunity Owner)" if case["is_owner"] == "Yes" else ""}</div>',
                unsafe_allow_html=True)

    main, side = st.columns([3, 2], gap="large")
    with main:
        render_assessment_summary(settings, case, a, sub, reviewer=True)
        if a["state"] != "COMPLETED" and case["status"] == "PENDING_REVIEW":
            if st.button("Re-run automated assessment"):
                with st.spinner("Re-running assessment…"):
                    rerun_assessment(settings, case_id)
                st.rerun()
    with side:
        with st.container(border=True):
            _render_decision(settings, case, a, sub["answers"])

    section("Supporting information")
    render_supporting(settings, case, sub, a)
