"""AI-Assisted Conflict of Interest Assessment — Proof of Concept (Streamlit entry point).

Run:  streamlit run app.py
Deep link:  http://localhost:8501/?page=reviewer&case=COI-0007
"""
from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent))

st.set_page_config(page_title="Conflict of Interest Assessment", page_icon="🛡️", layout="wide")

from coi.config import BANNER, LEARNING_STATEMENT, get_settings  # noqa: E402
from coi.db import init_db, simulate_ai_failure  # noqa: E402
from ui import admin_page, history_page, outbox_page, precedents_page, reviewer_page, submit_page  # noqa: E402
from ui.components import (CSS, MORE_PAGES, PAGES, PRIMARY_PAGES, RESPONDENT, REVIEWER, go,  # noqa: E402
                           set_query)

settings = get_settings()


@st.cache_resource
def _init(db_path: str) -> bool:
    init_db(settings)
    return True


_init(settings.db_path)
st.markdown(CSS, unsafe_allow_html=True)

# ---- Apply URL query parameters (deep links) whenever they change
qp = st.query_params
sig = (qp.get("page"), qp.get("case"), qp.get("resubmit"))
if st.session_state.get("_qp_sig") != sig:
    st.session_state["_qp_sig"] = sig
    page = qp.get("page")
    if page in PAGES:
        st.session_state["nav"] = page
        if page != "submit":
            st.session_state["persona"] = REVIEWER
        elif qp.get("resubmit"):
            st.session_state["persona"] = RESPONDENT  # respondent answering an information request
    if qp.get("case"):
        st.session_state["selected_case"] = qp.get("case")

# Default: the compliance view, opening on the declaration (Assessment) page.
st.session_state.setdefault("persona", REVIEWER)
st.session_state.setdefault("nav", "submit")
st.session_state.setdefault("reviewer_name", "Demo Reviewer")


def _on_persona_change() -> None:
    if st.session_state["persona"] == RESPONDENT:
        st.session_state["nav"] = "submit"
        set_query(page="submit")


def _on_nav_change() -> None:
    chosen = st.session_state.get("nav_radio")
    if not chosen:
        return
    page = {v: k for k, v in PAGES.items()}[chosen]
    st.session_state["nav"] = page
    case = st.session_state.get("selected_case") if page == "reviewer" else None
    set_query(page=page, case=case)


# ---- Sidebar (minimal)
with st.sidebar:
    st.markdown("### Conflict of Interest")
    st.radio("Viewing as", [RESPONDENT, REVIEWER], key="persona", on_change=_on_persona_change,
             help="No sign-in in this demo. Respondents see only a confirmation; the compliance reviewer sees the assessment.")
    is_reviewer = st.session_state["persona"] == REVIEWER
    allowed = PRIMARY_PAGES + MORE_PAGES if is_reviewer else ["submit"]
    if st.session_state["nav"] not in allowed:
        st.session_state["nav"] = "submit"
    primary = PRIMARY_PAGES if is_reviewer else ["submit"]
    st.session_state["nav_radio"] = PAGES[st.session_state["nav"]] if st.session_state["nav"] in primary else None
    st.radio("Go to", [PAGES[p] for p in primary], key="nav_radio", on_change=_on_nav_change, index=None)

    if is_reviewer:
        with st.expander("More"):
            for p in MORE_PAGES:
                st.button(PAGES[p], key=f"more_{p}", on_click=go, args=(p,), use_container_width=True)

    with st.expander("Demo controls"):
        st.selectbox("Demo scenario", list(submit_page.scenario_titles()), index=None, key="scenario_pick",
                     placeholder="Choose a scenario…")
        c1, c2 = st.columns(2)
        c1.button("Load", on_click=submit_page.load_scenario, use_container_width=True)
        c2.button("Clear form", on_click=submit_page.clear_form_and_result, use_container_width=True)
        st.selectbox("Opportunity preset", list(submit_page.opportunity_labels()), index=None, key="opp_pick",
                     placeholder="Choose an opportunity…")
        st.button("Prefill opportunity", on_click=submit_page.apply_opportunity, use_container_width=True)
        st.caption("Scenario 8 needs an upload: samples/sample_intermediary_declaration.pdf")

    with st.expander("About this demo"):
        st.caption(BANNER)
        st.caption(f"{LEARNING_STATEMENT} The underlying model is never retrained.")
        if settings.demo_mode:
            st.caption("Demo Mode is ON: assessments use a clearly labelled rules-based fallback, not AI.")

    if not settings.demo_mode and not settings.groq_api_key:
        st.warning("Automated assessment is not configured — cases will be routed to manual review. "
                   "Set GROQ_API_KEY, or DEMO_MODE=true.")
    if simulate_ai_failure(settings):
        st.warning("Simulated AI failure is ON (Admin tools).")

{
    "submit": submit_page.render,
    "reviewer": reviewer_page.render,
    "history": history_page.render,
    "precedents": precedents_page.render,
    "outbox": outbox_page.render,
    "admin": admin_page.render,  # the only page behind a sign-in
}[st.session_state["nav"]](settings)
