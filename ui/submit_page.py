"""ASSESSMENT page: declaration -> Submit for Assessment -> result shown on the same page.

The questionnaire is NOT inside st.form, so conditional questions appear/disappear immediately.
At submission, only currently visible answers are persisted (filtering happens in
coi.questionnaire.validate_submission), so stale hidden answers can never be submitted.

Respondent persona: sees only the confirmation + case ID (never the assessment).
Compliance reviewer persona: sees the assessment result inline, with one click to review the case.
"""
from __future__ import annotations

import json
from datetime import date

import streamlit as st

from coi import demo
from coi.config import Settings
from coi.db import get_case, latest_assessment, latest_submission, one
from coi.pipeline import resubmit_declaration, submit_declaration
from coi.questionnaire import (CONTEXT_FIELDS, INTERMEDIARY_BLOCK_MESSAGE, SOE_RELATIONSHIPS, answer_to_str,
                               compute_recency, is_visible, multiselect_items, question, questions)
from ui.components import REVIEWER, go, page_header, qlabel, section, set_query
from ui.result_view import render_assessment_summary, render_supporting, submission_for

UPLOAD_TYPES = ["pdf", "docx", "jpg", "jpeg", "png"]
TITLE = "Conflict of Interest Assessment"
SUBTITLE = "Complete the declaration below to assess potential conflicts relating to this opportunity."


def _k(qid: str) -> str:
    return f"q_{qid}"


def _upload_key() -> str:
    return f"upload_{st.session_state.get('upload_nonce', 0)}"


def _clear_form() -> None:
    """Clears declaration widgets. Safe to call inline before the page's widgets render."""
    for key in list(st.session_state.keys()):
        if key.startswith(("q_", "ctx_")):
            del st.session_state[key]
    st.session_state["upload_nonce"] = st.session_state.get("upload_nonce", 0) + 1


def _widget_value(qid: str, value: str):
    itype = question(qid)["input_type"]
    if itype == "date" and value:
        return date.fromisoformat(value)
    if itype == "multiselect":
        options = question(qid).get("options", [])
        return [v for v in multiselect_items(value) if v in options]
    return value


def _fill(context: dict, answers: dict) -> None:
    for f, v in context.items():
        st.session_state[f"ctx_{f}"] = v
    for qid, v in answers.items():
        if question(qid)["input_type"] not in ("file", "derived"):
            st.session_state[_k(qid)] = _widget_value(qid, v)


# ---------------------------------------------------------------- demo controls (sidebar callbacks)

def scenario_titles() -> dict[str, str]:
    return {s["title"]: k for k, s in demo.scenarios().items()}


def opportunity_labels() -> dict[str, str]:
    return {f"{o['opportunity_id']} · {o['opportunity_name']}": k for k, o in demo.opportunities().items()}


def _to_assessment_page() -> None:
    st.session_state["submit_result"] = None
    st.session_state["_resubmit_loaded"] = None
    st.session_state["nav"] = "submit"
    set_query(page="submit")


def load_scenario() -> None:
    key = scenario_titles().get(st.session_state.get("scenario_pick"))
    if not key:
        return
    _clear_form()
    _fill(demo.scenario_context(key), demo.scenario_answers(key))
    _to_assessment_page()


def apply_opportunity() -> None:
    key = opportunity_labels().get(st.session_state.get("opp_pick"))
    if not key:
        return
    o = demo.opportunities()[key]
    for f in ("opportunity_id", "opportunity_name", "client", "soe", "soe_relationship"):
        st.session_state[f"ctx_{f}"] = o[f]
    st.session_state["ctx_description"] = o["description"].strip()
    _to_assessment_page()


def clear_form_and_result() -> None:
    _clear_form()
    st.session_state["opp_pick"] = None
    st.session_state["scenario_pick"] = None
    _to_assessment_page()


def _new_declaration() -> None:
    _clear_form()
    _to_assessment_page()


# ---------------------------------------------------------------- form

def _render_question(q: dict, container, values: dict):
    qid, itype = q["id"], q["input_type"]
    label = f"**Q{q['number']}** — {q['text']}"
    key = _k(qid)
    if itype == "yes_no":
        return container.radio(label, ["Yes", "No"], index=None, horizontal=True, key=key)
    if itype == "select":
        return container.selectbox(label, q["options"], index=None, placeholder="Select…", key=key)
    if itype == "multiselect":
        return container.multiselect(label, q["options"], key=key)
    if itype == "text":
        return container.text_input(label, key=key)
    if itype == "textarea":
        return container.text_area(label, key=key, height=90)
    if itype == "email_list":
        return container.text_area(label, key=key, height=70, placeholder="name@example.com, other@example.com")
    if itype == "date":
        return container.date_input(label, value=None, min_value=date(1960, 1, 1), max_value=date.today(),
                                    key=key, format="DD/MM/YYYY")
    if itype == "derived":
        last = values.get("PRIOR_EMPLOYMENT_LAST_DATE")
        if last:
            within = compute_recency(last, date.today()) == "<= 2 years"
            container.caption(f"Time since last working date (system-calculated): "
                              f"**{'2 years or less' if within else 'more than 2 years'}**")
        return None
    if itype == "file":
        return container.file_uploader(label, type=UPLOAD_TYPES, key=_upload_key())
    return None


def _render_form(settings: Settings, resubmit_case: dict | None, existing_upload: str | None) -> dict | None:
    """Renders the declaration. Returns the submission result on a successful submit, else None."""
    resubmitting = resubmit_case is not None

    section("Opportunity details")
    c1, c2 = st.columns(2)
    c1.text_input("Opportunity ID", key="ctx_opportunity_id")
    c2.text_input("Opportunity name", key="ctx_opportunity_name")
    c1.text_input("Client", key="ctx_client")
    c2.text_input("State-Owned Entity (SOE)", key="ctx_soe")
    c1.selectbox("Relationship of the SOE to the opportunity", SOE_RELATIONSHIPS, index=None,
                 placeholder="Select…", key="ctx_soe_relationship")
    c2.text_area("Short opportunity / service description", key="ctx_description", height=68)

    section("Respondent")
    c1, c2 = st.columns(2)
    c1.text_input("Name", key="ctx_respondent_name", disabled=resubmitting)
    c2.text_input("Email", key="ctx_respondent_email", disabled=resubmitting)
    c1.text_input("Role on this opportunity", key="ctx_respondent_role")
    c2.radio("Opportunity Owner?", ["Yes", "No"], index=None, horizontal=True, key="ctx_is_owner",
             disabled=resubmitting)

    section("Conflict of Interest declaration")
    is_owner = st.session_state.get("ctx_is_owner") == "Yes"
    values: dict[str, str] = {}
    upload_file = None
    for q in questions():
        if not is_visible(q, values, is_owner):
            continue
        if q.get("section"):
            st.markdown(f"**{q['section']}**")
        container = st.columns([1, 24])[1] if q.get("show_if") else st.container()
        val = _render_question(q, container, values)
        if q["input_type"] == "file":
            upload_file = val
            if existing_upload and not val:
                container.caption(f"Previously uploaded: {existing_upload} (kept unless you upload a replacement)")
            if not val and not existing_upload:
                container.warning(INTERMEDIARY_BLOCK_MESSAGE)
            continue
        if q["input_type"] == "derived":
            continue
        s = answer_to_str(q, val)
        if s is not None:
            values[q["id"]] = s
    if not is_owner:
        st.caption("Section 6 applies to Opportunity Owners only.")

    st.write("")
    if st.button("Resubmit for Assessment" if resubmitting else "Submit for Assessment", type="primary"):
        raw = {q["id"]: st.session_state.get(_k(q["id"])) for q in questions() if q["input_type"] not in ("file", "derived")}
        context = {f: st.session_state.get(f"ctx_{f}") for f in CONTEXT_FIELDS}
        upload = ({"name": upload_file.name, "size": upload_file.size, "bytes": upload_file.getvalue()}
                  if upload_file else None)
        with st.spinner("Assessing your declaration…"):
            if resubmitting:
                res = resubmit_declaration(settings, resubmit_case["case_id"], context, raw, upload, existing_upload)
            else:
                res = submit_declaration(settings, context, raw, upload)
        if not res.ok:
            st.error("**Please correct the following before submitting:**\n\n" +
                     "\n".join(f"- {msg}" for _, msg in res.errors))
            return None
        return {"case_id": res.case_id, "review_required": res.review_required, "resubmitted": resubmitting}
    return None


# ---------------------------------------------------------------- after submission

def _render_respondent_confirmation(r: dict) -> None:
    st.success("Your Conflict of Interest declaration has been submitted for assessment.")
    st.markdown(f"#### Case reference: {r['case_id']}")
    if r["review_required"]:
        st.info("Your declaration is under review.")
    st.button("Start a new declaration", on_click=_new_declaration)


def _render_assessment_result(settings: Settings, r: dict) -> None:
    case = get_case(settings, r["case_id"])
    a = latest_assessment(settings, r["case_id"])
    sub = submission_for(settings, r["case_id"], a)
    st.markdown(f'<div class="coi-muted">Declaration submitted · Case <b>{case["case_id"]}</b> · '
                f'{case["opportunity_name"]}</div>', unsafe_allow_html=True)
    render_assessment_summary(settings, case, a, sub, reviewer=False)
    st.write("")
    c1, c2, _ = st.columns([1, 1, 2])
    if case["status"] == "PENDING_REVIEW":
        c1.button("Review Case", type="primary", on_click=go, args=("reviewer", case["case_id"]),
                  use_container_width=True)
    c2.button("New declaration", on_click=_new_declaration, use_container_width=True)
    st.caption("The respondent sees only a confirmation and case reference — the assessment is visible to the "
               "compliance team.")
    section("Supporting information")
    render_supporting(settings, case, sub, a, audit=False)


def _render_outcome(settings: Settings, r: dict) -> None:
    if st.session_state.get("persona") == REVIEWER:
        _render_assessment_result(settings, r)
    else:
        _render_respondent_confirmation(r)


def render(settings: Settings) -> None:
    r = st.session_state.get("submit_result")
    if r:
        page_header(TITLE, "Assessment result for the submitted declaration." if
                    st.session_state.get("persona") == REVIEWER else SUBTITLE, settings.demo_mode)
        _render_outcome(settings, r)
        return

    resubmit_id = st.query_params.get("resubmit")
    resubmit_case, existing_upload = None, None
    if resubmit_id:
        page_header("Update your declaration", "The reviewer has asked for more information — update your answers "
                    "and resubmit.", settings.demo_mode)
        resubmit_case = get_case(settings, resubmit_id)
        if not resubmit_case:
            st.error(f"Case {resubmit_id} was not found.")
            return
        if resubmit_case["status"] != "AWAITING_INFORMATION":
            st.warning(f"Case {resubmit_id} is not awaiting information.")
            st.button("Start a new declaration", on_click=_new_declaration)
            return
        sub = latest_submission(settings, resubmit_id)
        existing_upload = sub["answers"].get("INTERMEDIARY_DECLARATION_FILE")
        if st.session_state.get("_resubmit_loaded") != resubmit_id:
            _clear_form()
            c = resubmit_case
            _fill({"opportunity_id": c["opportunity_id"], "opportunity_name": c["opportunity_name"], "client": c["client"],
                   "soe": c["soe"], "soe_relationship": c["soe_relationship"], "description": c["opportunity_description"],
                   "respondent_name": c["respondent_name"], "respondent_email": c["respondent_email"],
                   "respondent_role": c["respondent_role"], "is_owner": c["is_owner"]}, sub["answers"])
            st.session_state["_resubmit_loaded"] = resubmit_id
        req = one(settings, "SELECT * FROM decisions WHERE case_id=? AND action='REQUEST_MORE_INFORMATION' "
                            "ORDER BY id DESC LIMIT 1", (resubmit_id,))
        info = json.loads(req["info_request_json"]) if req else {"fields": [], "message": ""}
        st.info(f"**Information requested for case {resubmit_id}**\n\n> {info['message']}\n\n"
                f"**Please review:** " + ", ".join(qlabel(f) for f in info["fields"]))
    else:
        page_header(TITLE, SUBTITLE, settings.demo_mode)

    # The form lives in a placeholder: after a successful submit it is cleared and the result is rendered in its
    # place in the same run (no mid-run st.rerun), so the result appears immediately on the same page.
    holder = st.empty()
    with holder.container():
        submitted = _render_form(settings, resubmit_case, existing_upload)
    if submitted:
        holder.empty()
        st.session_state["submit_result"] = submitted
        _render_outcome(settings, submitted)
