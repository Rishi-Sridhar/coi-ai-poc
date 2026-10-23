"""EMAIL OUTBOX page: every notification the application prepared, with an HTML preview (open to all; no sign-in)."""
from __future__ import annotations

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

from coi.config import Settings
from coi.db import rows
from ui.components import DELIVERY_DISPLAY, email_preview_html, fmt_time, page_header


def render(settings: Settings) -> None:
    page_header("Email outbox", "Notifications prepared by the application.", settings.demo_mode)
    st.caption("Every email is recorded here before any delivery attempt, so the demo works without an email server.")
    emails = rows(settings, "SELECT * FROM emails ORDER BY id DESC")
    if not emails:
        st.info("No emails yet.")
        return
    st.dataframe(pd.DataFrame([{
        "Time": fmt_time(e["created_at"]), "To": e["recipient"], "Case": e["case_id"] or "",
        "Subject": e["subject"], "Status": DELIVERY_DISPLAY.get(e["delivery_status"], e["delivery_status"]),
    } for e in emails]), hide_index=True, use_container_width=True)
    by_label = {f"{x['subject']}  ·  {fmt_time(x['created_at'])}": x for x in emails}
    e = by_label[st.selectbox("Preview email", list(by_label))]
    st.markdown(f"**To:** {e['recipient']} · **Subject:** {e['subject']}")
    components.html(email_preview_html(e["body_html"]), height=720, scrolling=True)
    with st.expander("Plain-text version and delivery diagnostics"):
        st.markdown(f"Delivery status code: `{e['delivery_status']}`" + (f" · Error: `{e['error']}`" if e["error"] else ""))
        st.code(e["body_text"] or "", language=None)
