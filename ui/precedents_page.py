"""REFERENCE CASES page (precedent library / learning) — plain-language first, mechanics behind expanders."""
from __future__ import annotations

import pandas as pd
import streamlit as st

from coi.actions import action_label
from coi.config import LEARNING_STATEMENT, Settings
from coi.db import list_precedents
from coi.review import set_precedent_active
from coi.display import OUTCOME_DISPLAY
from ui.components import fmt_time, go, page_header, synthetic_notice


def render(settings: Settings) -> None:
    page_header("Reference cases", "Reviewer-approved decisions can be used as reference cases for future assessments.",
                settings.demo_mode)
    synthetic_notice()

    precs = list_precedents(settings)
    m = st.columns(2)
    m[0].metric("Synthetic reference cases", sum(p["source"] == "SYNTHETIC" for p in precs))
    m[1].metric("Reviewer-approved reference cases", sum(p["source"] == "REVIEWER" for p in precs))

    source = st.radio("Show", ["All", "Reviewer-approved", "Synthetic"], horizontal=True)
    shown = [p for p in reversed(precs) if source == "All" or
             (source == "Reviewer-approved" and p["source"] == "REVIEWER") or
             (source == "Synthetic" and p["source"] == "SYNTHETIC")]
    st.dataframe(pd.DataFrame([{
        "Reference": p["id"], "Type": "Reviewer-approved" if p["source"] == "REVIEWER" else "Synthetic example",
        "Outcome": OUTCOME_DISPLAY.get(p["outcome"], p["outcome"]),
        "Final action": ", ".join(action_label(c) for c in p["snapshot"].get("final_actions", [])),
        "Summary": p["snapshot"].get("title"),
        "Rationale": p["rationale"], "In use": "Yes" if p["active"] else "Retired", "Added": fmt_time(p["created_at"]),
    } for p in shown]), hide_index=True, use_container_width=True)

    with st.expander("Reference case details & retire / reactivate"):
        options = {f"{p['id']} — {p['snapshot'].get('title')}": p for p in shown}
        if options:
            p = options[st.selectbox("Reference case", list(options))]
            final_actions = ", ".join(action_label(c) for c in p["snapshot"].get("final_actions", [])) or "—"
            st.markdown(f"**Outcome:** {OUTCOME_DISPLAY.get(p['outcome'], p['outcome'])}  \n"
                        f"**Final action:** {final_actions}  \n"
                        f"**Opportunity summary:** {p['snapshot'].get('opportunity_summary')}  \n"
                        f"**Rationale:** {p['rationale']}")
            st.caption("Synthetic demonstration example." if p["source"] == "SYNTHETIC"
                       else "Anonymised snapshot of a reviewer decision (no respondent name or email).")
            st.json(p["snapshot"].get("answers", {}), expanded=False)
            actor = st.session_state.get("reviewer_name", "reviewer")
            c1, c2, c3 = st.columns([2, 1, 1])
            if p["active"]:
                reason = c1.text_input("Reason for retiring (optional)", key=f"reason_{p['id']}")
                if c2.button("Retire", key=f"retire_{p['id']}"):
                    set_precedent_active(settings, p["id"], False, actor, reason)
                    st.rerun()
            else:
                c1.caption(f"Retired {fmt_time(p['retired_at'])}: {p['retired_reason']}")
                if c2.button("Reactivate", key=f"react_{p['id']}"):
                    set_precedent_active(settings, p["id"], True, actor)
                    st.rerun()
            if p["case_id"]:
                c3.button("Open source case", key=f"open_{p['id']}", on_click=go, args=("reviewer", p["case_id"]))

    with st.expander("How reference cases are used"):
        st.markdown(
            f"**{LEARNING_STATEMENT}**\n\n"
            "The POC does not retrain the foundation model. Reviewer-approved decisions become reference cases that are "
            "provided to subsequent assessments. This gives immediately visible human-in-the-loop adaptation while also "
            "building a labelled dataset that could later support formal model training once sufficient validated "
            "real-world data exists.")
        st.markdown(
            "**Technical detail — matching (no embeddings, no vector database):** every conflict-related Yes/No answer, "
            "the Opportunity Owner flag and the SOE relationship are compared. Both *Yes* → full field weight; both *No* "
            "→ 0.25 × weight; disagreement → 0. Scores are normalised 0–1 against a perfect match; the top 3 active "
            "cases with score > 0 are used. Ties: reviewer-approved before synthetic, then newer first.")
