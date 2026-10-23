"""Shared Streamlit UI helpers: navigation, business wording, styling, timestamps."""
from __future__ import annotations

from datetime import datetime
from html import escape

import streamlit as st

from coi.emailer import ENGINE_LABEL_FALLBACK, ENGINE_LABEL_LLM
from coi.questionnaire import question

# Internal page keys are unchanged so deep links (?page=reviewer&case=COI-0007) keep working.
PAGES = {
    "submit": "Assessment",
    "reviewer": "Review",
    "history": "Case history",
    "precedents": "Reference cases",
    "outbox": "Email outbox",
    "admin": "Admin (sign-in required)",
}
PRIMARY_PAGES = ["submit", "reviewer"]
MORE_PAGES = ["history", "precedents", "outbox", "admin"]
RESPONDENT, REVIEWER = "Respondent", "Compliance reviewer"

DEMO_DISCLOSURE = "Demo environment · Synthetic test data"

# ---- business wording for internal codes (codes themselves are unchanged)
STATUS_DISPLAY = {
    "PENDING_REVIEW": "Pending compliance review",
    "AWAITING_INFORMATION": "Awaiting information from respondent",
    "CLOSED": "Closed",
    "NOT_REVIEWED": "No review required",
    "SUBMITTED": "Submitted",
}
EVENT_LABELS = {
    "SUBMITTED": "Declaration submitted",
    "RESUBMITTED": "Updated declaration submitted",
    "QUALITY_CHECKED": "Response quality checked",
    "ASSESSED": "Assessment completed",
    "AI_FAILED": "Automated assessment unavailable",
    "ROUTED": "Case routed",
    "EMAIL_QUEUED": "Notification prepared",
    "DECISION_RECORDED": "Reviewer decision recorded",
    "INFO_REQUESTED": "Additional information requested",
    "PRECEDENT_ADDED": "Saved as reference case",
    "PRECEDENT_RETIRED": "Reference case retired",
    "PRECEDENT_REACTIVATED": "Reference case reactivated",
    "REASSESSMENT_REQUESTED": "Assessment re-run requested",
}
ACTION_DISPLAY = {"CONFIRM": "Confirmed preliminary assessment", "OVERRIDE": "Overrode preliminary assessment",
                  "REQUEST_MORE_INFORMATION": "Requested more information"}
DELIVERY_DISPLAY = {"SENT": "Sent", "FAILED": "Prepared — delivery failed", "NOT_CONFIGURED": "Prepared (email sending not configured)",
                    "QUEUED": "Prepared"}

CSS = """
<style>
/* Clean business screens: hide Streamlit's deploy button and top decoration bar. */
.stDeployButton, [data-testid="stAppDeployButton"], [data-testid="stDecoration"] {display:none !important;}
.block-container {padding-top: 2.2rem; max-width: 1100px;}
h1 {font-weight: 650; letter-spacing: -0.01em;}
.coi-sub {color:#475467; margin:-0.6rem 0 0.2rem 0; font-size:1.02rem;}
.coi-demo {color:#98a2b3; font-size:0.78rem; letter-spacing:.02em; margin-bottom:1.2rem;}
.coi-section {font-size:0.78rem; font-weight:650; letter-spacing:.08em; text-transform:uppercase; color:#475467;
              margin:1.6rem 0 0.4rem 0; padding-bottom:0.35rem; border-bottom:1px solid #eaecf0;}
.coi-verdict {border-left:6px solid; border-radius:6px; padding:16px 20px; margin:0.6rem 0 1rem 0;}
.coi-verdict .t {font-size:1.55rem; font-weight:700; line-height:1.2;}
.coi-verdict .s {font-size:0.85rem; color:#475467; margin-top:4px;}
.v-danger {border-color:#b42318; background:#fef3f2;} .v-danger .t {color:#912018;}
.v-ok {border-color:#067647; background:#ecfdf3;} .v-ok .t {color:#05603a;}
.v-warn {border-color:#b54708; background:#fffaeb;} .v-warn .t {color:#93370d;}
.v-neutral {border-color:#667085; background:#f2f4f7;} .v-neutral .t {color:#344054;}
.coi-label {font-size:0.72rem; font-weight:650; letter-spacing:.08em; text-transform:uppercase; color:#667085;
            margin:1.1rem 0 0.35rem 0;}
.coi-text {font-size:0.98rem; color:#101828; line-height:1.5;}
.coi-resp {display:flex; gap:16px; padding:8px 0; border-bottom:1px solid #f2f4f7;}
.coi-resp .q {flex:0 0 50%; color:#344054;} .coi-resp .q b {color:#101828;}
.coi-resp .a {flex:1; font-weight:600; color:#101828; word-break:break-word;}
.coi-resp .w {display:block; font-size:0.82rem; color:#667085; font-weight:400; margin-top:2px;}
.coi-issue {border:1px solid #fedf89; background:#fffcf5; border-radius:6px; padding:12px 16px; margin:6px 0 10px 0;}
.coi-issue .h {font-weight:650; color:#93370d; margin-bottom:6px;}
.coi-quote {font-style:italic; color:#101828; background:#fff; border-left:3px solid #fdb022; padding:4px 10px; margin:4px 0 8px 0;}
.coi-muted {color:#667085; font-size:0.85rem;}
.coi-verdict .k {font-size:0.7rem; font-weight:700; letter-spacing:.1em; text-transform:uppercase; color:#475467; margin-bottom:4px;}
.coi-verdict .ic {display:inline-block; min-width:1.6rem; margin-right:0.35rem; font-weight:700;}
.coi-verdict .d {font-size:1rem; color:#101828; margin-top:6px; line-height:1.45;}
.coi-card {border:1px solid #eaecf0; border-radius:8px; padding:12px 14px; background:#fff; height:100%;}
.coi-card .n {font-size:0.75rem; font-weight:700; color:#475467;}
.coi-card .l {font-size:0.86rem; color:#344054; margin:2px 0 6px 0; line-height:1.3;}
.coi-card .v {font-size:1rem; font-weight:650; color:#101828; word-break:break-word; line-height:1.35;}
.coi-card .v.txt {font-weight:400; font-size:0.9rem; font-style:italic;}
.coi-card .f {font-size:0.75rem; color:#93370d; margin-top:6px; font-weight:600;}
.coi-card .m {font-size:0.72rem; color:#667085; margin-top:4px;}
.coi-other {font-size:0.9rem; color:#101828; padding:5px 0; border-bottom:1px solid #f2f4f7;}
.coi-other .q {color:#475467;} .coi-other .w {font-size:0.8rem; color:#667085; margin-top:1px;}
.coi-act {border:1px solid #eaecf0; border-left:4px solid #98a2b3; border-radius:6px; padding:8px 12px; margin:6px 0; background:#fff;}
.coi-act .t {font-weight:650; color:#101828; font-size:0.95rem;}
.coi-act .r {font-size:0.86rem; color:#475467; margin-top:2px; line-height:1.4;}
.coi-act.primary {padding:10px 14px;} .coi-act.primary .t {font-size:1.05rem;}
.coi-act.cat-mitigation {border-left-color:#d92d20;} .coi-act.cat-information {border-left-color:#f79009;}
.coi-act.cat-escalation {border-left-color:#475467;} .coi-act.cat-none {border-left-color:#12b76a;}
.coi-act-more {font-size:0.72rem; font-weight:650; letter-spacing:.06em; text-transform:uppercase; color:#667085; margin:10px 0 2px 0;}
.coi-overview {font-size:0.88rem; color:#475467; margin:0.9rem 0 0.3rem 0;}
.coi-overview b {color:#101828;}
.coi-ctx {font-size:0.88rem; color:#475467; line-height:1.55; padding-bottom:8px; border-bottom:1px solid #eaecf0; margin-bottom:6px;}
.coi-ctx b {color:#101828;}
.coi-dsec {font-size:0.8rem; font-weight:700; letter-spacing:.04em; color:#1d2939; margin:14px 0 4px 0;
           padding-bottom:4px; border-bottom:1px solid #f2f4f7;}
.drow {padding:6px 0 6px 10px; border-left:3px solid transparent;}
.drow.cited {border-left-color:#d0d5dd; background:#fcfcfd;}
.drow .dl {font-size:0.86rem; color:#344054;}
.drow .dl b {color:#101828; margin-right:4px;}
.drow .dv {font-size:0.95rem; font-weight:600; color:#101828; margin-top:2px; word-break:break-word;}
.drow .dv.txt {font-weight:400; white-space:pre-wrap; background:#f9fafb; border-radius:4px; padding:6px 8px;}
.tag-c, .tag-i {display:inline-block; font-size:0.68rem; font-weight:700; padding:1px 7px; border-radius:10px; margin-left:6px;
                vertical-align:middle;}
.tag-c {background:#eaecf0; color:#344054;}
/* Narrow screens: stack columns (assessment above decision, cards full width) instead of squeezing them. */
@media (max-width: 1100px) {
  [data-testid="stHorizontalBlock"] {flex-wrap: wrap !important;}
  [data-testid="column"], [data-testid="stColumn"] {flex: 1 1 100% !important; min-width: 100% !important; width: 100% !important;}
}
.tag-i {background:#fef0c7; color:#93370d;}
.coi-pill {display:inline-block; padding:2px 10px; border-radius:12px; font-size:0.8rem; font-weight:600;
           background:#f2f4f7; color:#344054; margin-right:6px;}
.coi-kv {color:#475467; font-size:0.92rem; line-height:1.6;}
.coi-kv b {color:#101828;}
</style>
"""


def page_header(title: str, subtitle: str, demo_mode: bool) -> None:
    """Title, one-sentence subtitle and the subtle demo disclosure (full disclaimer: sidebar > About this demo)."""
    st.title(title)
    st.markdown(f'<div class="coi-sub">{escape(subtitle)}</div>', unsafe_allow_html=True)
    note = DEMO_DISCLOSURE + (" · Demo Mode (rules-based assessment)" if demo_mode else "")
    st.markdown(f'<div class="coi-demo">{escape(note)}</div>', unsafe_allow_html=True)


def email_preview_html(body_html: str | None) -> str:
    """Email HTML for an in-app preview frame: every link opens in a new browser tab, not inside the frame."""
    return '<base target="_blank">' + (body_html or "")


def section(title: str) -> None:
    st.markdown(f'<div class="coi-section">{escape(title)}</div>', unsafe_allow_html=True)


def label(title: str) -> None:
    st.markdown(f'<div class="coi-label">{escape(title)}</div>', unsafe_allow_html=True)


def pill(text: str) -> str:
    return f'<span class="coi-pill">{escape(text)}</span>'


def engine_label(engine: str | None) -> str:
    return ENGINE_LABEL_FALLBACK if engine == "DEMO_FALLBACK" else ENGINE_LABEL_LLM


def fmt_time(iso: str | None) -> str:
    if not iso:
        return ""
    try:
        return datetime.fromisoformat(iso).astimezone().strftime("%d %b %Y, %H:%M")
    except ValueError:
        return iso


def qlabel(qid: str) -> str:
    try:
        q = question(qid)
        return f"Q{q['number']} · {q['label']}"
    except KeyError:
        return qid


def set_query(**params: str | None) -> None:
    st.query_params.clear()
    for k, v in params.items():
        if v:
            st.query_params[k] = v
    st.session_state["_qp_sig"] = (params.get("page"), params.get("case"), params.get("resubmit"))


def go(page: str, case: str | None = None) -> None:
    """Navigation callback (use with on_click so widget-backed state can be changed safely)."""
    st.session_state["nav"] = page
    if page != "submit":
        st.session_state["persona"] = REVIEWER
    if case:
        st.session_state["selected_case"] = case
    set_query(page=page, case=case if page == "reviewer" else None)


def synthetic_notice() -> None:
    st.caption("Synthetic reference cases (SYN-…) are demonstration examples only — they are not real cases and do not "
               "represent EY decisions or policy.")
