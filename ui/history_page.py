"""CASE HISTORY page: all cases. Preliminary assessment, case status and the reviewer's final decision are
kept as separate columns (never merged)."""
from __future__ import annotations

import pandas as pd
import streamlit as st

from coi.config import Settings
from coi.db import rows
from coi.display import OUTCOME_DISPLAY, VERDICT_DISPLAY
from ui.components import STATUS_DISPLAY, fmt_time, go, page_header

SQL = """
SELECT c.case_id, c.created_at, c.opportunity_id, c.opportunity_name, c.respondent_name, c.respondent_role,
       c.status,
       (SELECT verdict FROM assessments a WHERE a.case_id=c.case_id ORDER BY a.id DESC LIMIT 1) AS ai_verdict,
       (SELECT final_outcome FROM decisions d WHERE d.case_id=c.case_id AND d.final_outcome IS NOT NULL
         ORDER BY d.id DESC LIMIT 1) AS final_outcome,
       (SELECT agrees_with_ai FROM decisions d WHERE d.case_id=c.case_id AND d.final_outcome IS NOT NULL
         ORDER BY d.id DESC LIMIT 1) AS agrees
FROM cases c ORDER BY c.case_id DESC
"""


def render(settings: Settings) -> None:
    page_header("Case history", "All submitted declarations and where each one stands.", settings.demo_mode)
    cases = rows(settings, SQL)
    if not cases:
        st.info("No declarations have been submitted yet.")
        return

    c1, c2 = st.columns([2, 1])
    target = c1.selectbox("Open a case", [c["case_id"] for c in cases], index=None, placeholder="Select case…")
    c2.write("")
    c2.button("Open in Review →", on_click=go, args=("reviewer", target), disabled=not target, use_container_width=True)

    statuses = sorted({STATUS_DISPLAY.get(c["status"], c["status"]) for c in cases})
    pick = st.multiselect("Status", statuses, default=statuses)
    df = pd.DataFrame([{
        "Case": c["case_id"], "Submitted": fmt_time(c["created_at"]), "Opportunity": c["opportunity_name"],
        "Respondent": f"{c['respondent_name']} ({c['respondent_role']})",
        "Status": STATUS_DISPLAY.get(c["status"], c["status"]),
        "Preliminary assessment": VERDICT_DISPLAY.get(c["ai_verdict"], (c["ai_verdict"] or "",))[0],
        "Final decision": OUTCOME_DISPLAY.get(c["final_outcome"], "") if c["final_outcome"] else "",
        "Review link": f"{settings.app_base_url}/?page=reviewer&case={c['case_id']}",
    } for c in cases if STATUS_DISPLAY.get(c["status"], c["status"]) in pick])
    st.dataframe(df, hide_index=True, use_container_width=True,
                 column_config={"Review link": st.column_config.LinkColumn("Review link", display_text="open")})

    decided = [c for c in cases if c["agrees"] is not None]
    if decided:
        st.caption(f"Reviewers agreed with the preliminary assessment in {sum(c['agrees'] for c in decided)} of "
                   f"{len(decided)} decided cases.")
