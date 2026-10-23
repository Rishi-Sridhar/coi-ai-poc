"""ADMIN page (the only page behind a sign-in): demo tools (simulate AI failure, reset, SMTP test) and technical settings.

Everything sensitive is rendered only after sign-in, and each action re-checks the session before acting."""
from __future__ import annotations

import time

import pandas as pd
import streamlit as st

from coi import admin_auth
from coi.config import Settings
from coi.db import reset_demo, rows, set_setting, simulate_ai_failure
from coi.emailer import send_email
from ui.components import DELIVERY_DISPLAY, EVENT_LABELS, fmt_time, page_header

AUTH_KEY = "_admin_until"   # session_state: epoch seconds until which this browser session is signed in


def _signed_in() -> bool:
    return st.session_state.get(AUTH_KEY, 0) > time.time()


def _client_ip() -> str:
    try:  # st.context needs Streamlit >= 1.37
        fwd = st.context.headers.get("X-Forwarded-For", "")
        return fwd.split(",")[0].strip() or "unknown"
    except Exception:  # noqa: BLE001 - logging aid only
        return "unknown"


def _sign_out() -> None:
    st.session_state.pop(AUTH_KEY, None)


def _gate(settings: Settings) -> bool:
    """True if this session is signed in (extends the session); otherwise renders the sign-in form and returns False."""
    now = time.time()
    if _signed_in():
        st.session_state[AUTH_KEY] = now + admin_auth.SESSION_SECONDS
        return True
    st.session_state.pop(AUTH_KEY, None)
    if not admin_auth.is_configured(settings.admin_password_hash):
        st.warning("Admin sign-in is not configured on this deployment, so the Admin page is locked.")
        return False
    st.markdown("This page holds sensitive demo controls. Sign in to continue. All other pages are open.")
    with st.form("admin_login"):
        password = st.text_input("Admin password", type="password")
        submitted = st.form_submit_button("Sign in")
    wait = admin_auth.THROTTLE.locked_for(now)
    if wait:
        st.error(f"Too many failed attempts. Try again in {-(-wait // 60)} minute(s).")
    elif submitted:
        if admin_auth.verify_password(password, settings.admin_password_hash):
            admin_auth.THROTTLE.record_success()
            st.session_state[AUTH_KEY] = time.time() + admin_auth.SESSION_SECONDS
            print(f"[admin-auth] sign-in ok from {_client_ip()}", flush=True)
            st.rerun()
        admin_auth.THROTTLE.record_failure(time.time())
        print(f"[admin-auth] sign-in FAILED from {_client_ip()}", flush=True)
        st.error("Incorrect password.")
    return False


def _toggle_failure(settings: Settings) -> None:
    if _signed_in():
        set_setting(settings, "simulate_ai_failure", "true" if st.session_state["sim_fail"] else "false")


def render(settings: Settings) -> None:
    page_header("Admin", "Sensitive demo controls and technical settings. Sign-in required.", settings.demo_mode)
    if not _gate(settings):
        return
    st.button("Sign out", on_click=_sign_out)
    tab_controls, tab_config = st.tabs(["Demo tools", "Technical settings"])

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
            if _signed_in():
                reset_demo(settings)
                for k in ("selected_case", "case_pick", "submit_result", "_resubmit_loaded"):
                    st.session_state.pop(k, None)
                st.success("Demo data reset. Synthetic reference cases restored; the next case will be COI-0001.")

        st.markdown("**Email delivery test**")
        if st.button("Send test email to the reviewer address") and _signed_in():
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
