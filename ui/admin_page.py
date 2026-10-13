"""EMAIL OUTBOX & ADMIN page: outbox, demo tools (simulate AI failure, reset, SMTP test), technical configuration."""
from __future__ import annotations

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

from coi.config import Settings
from coi.db import reset_demo, rows, set_setting, simulate_ai_failure
from coi.emailer import send_email
from ui.components import DELIVERY_DISPLAY, EVENT_LABELS, email_preview_html, fmt_time, page_header


def _toggle_failure(settings: Settings) -> None:
    set_setting(settings, "simulate_ai_failure", "true" if st.session_state["sim_fail"] else "false")


def render(settings: Settings) -> None:
    page_header("Email outbox & admin", "Notifications prepared by the application, demo tools and technical settings.",
                settings.demo_mode)
    tab_outbox, tab_controls, tab_config = st.tabs(["Email outbox", "Demo tools", "Technical settings"])

    with tab_outbox:
        st.caption("Every email is recorded here before any delivery attempt, so the demo works without an email server.")
        emails = rows(settings, "SELECT * FROM emails ORDER BY id DESC")
        if not emails:
            st.info("No emails yet.")
        else:
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

    with tab_controls:
        st.markdown("**Simulate AI failure**")
        st.toggle("Deliberately make the automated assessment fail for new submissions",
                  value=simulate_ai_failure(settings), key="sim_fail", on_change=_toggle_failure, args=(settings,))
        st.caption("Demonstrates the fail-safe: cases become *Automated Assessment Unavailable* and go to manual review; "
                   "the case, email and review still work.")

        st.markdown("**Reset demo data**")
        st.caption("Removes demo-created cases, decisions, reviewer reference cases, emails, audit history and uploads. "
                   "Restores the 15 synthetic reference cases. Case numbering restarts at COI-0001.")
        confirm = st.checkbox("I understand this permanently deletes all demo-created data.", key="reset_confirm")
        typed = st.text_input("Type RESET to confirm", key="reset_typed")
        if st.button("Reset demo data", type="primary", disabled=not (confirm and typed.strip() == "RESET")):
            reset_demo(settings)
            for k in ("selected_case", "case_pick", "submit_result", "_resubmit_loaded"):
                st.session_state.pop(k, None)
            st.success("Demo data reset. Synthetic reference cases restored; the next case will be COI-0001.")

        st.markdown("**Email delivery test**")
        if st.button("Send test email to the reviewer address"):
            res = send_email(settings, None, settings.reviewer_email, "SMTP_TEST", "[COI POC] SMTP test email",
                             "<p>This is a test email from the COI Assessment POC.</p>",
                             "This is a test email from the COI Assessment POC.")
            st.write(f"Result: **{DELIVERY_DISPLAY.get(res['delivery_status'], res['delivery_status'])}** {res['error'] or ''}")

    with tab_config:
        cfg = {
            "DEMO_MODE": settings.demo_mode,
            "GROQ_API_KEY configured": bool(settings.groq_api_key),
            "GROQ_MODEL": settings.groq_model,
            "SMTP configured (SMTP_HOST)": settings.smtp_configured,
            "SMTP_HOST:PORT": f"{settings.smtp_host or '–'}:{settings.smtp_port}",
            "SMTP_FROM_EMAIL": settings.smtp_from_email or "–",
            "REVIEWER_EMAIL": settings.reviewer_email,
            "APP_BASE_URL": settings.app_base_url,
            "REVIEW_CLEAN_CASES": settings.review_clean_cases,
            "PROMPT_VERSION": settings.prompt_version,
            "GUIDANCE_VERSION": settings.guidance_version,
            "QUESTIONNAIRE_VERSION": settings.questionnaire_version,
            "Database": settings.db_path,
        }
        st.dataframe(pd.DataFrame([{"Setting": k, "Value": str(v)} for k, v in cfg.items()]), hide_index=True,
                     use_container_width=True)
        st.caption("Read from `.env` at startup (see `.env.example`); restart Streamlit after changes. Secrets are never displayed.")
        with st.expander("Recent audit events (all cases)"):
            events = rows(settings, "SELECT * FROM audit_events ORDER BY id DESC LIMIT 200")
            st.dataframe(pd.DataFrame([{"Time": fmt_time(e["created_at"]), "Case": e["case_id"] or "",
                                        "Event": EVENT_LABELS.get(e["event_type"], e["event_type"]),
                                        "Code": e["event_type"], "By": e["actor"], "Details": e["details_json"]}
                                       for e in events]), hide_index=True, use_container_width=True)
