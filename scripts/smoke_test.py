"""Integration smoke tests for the risky dependencies: SQLite, Groq structured output, SMTP.

Run:  python scripts/smoke_test.py            (SQLite + Groq + SMTP connectivity, no email sent)
      python scripts/smoke_test.py --send     (also sends one test email to REVIEWER_EMAIL)
"""
from __future__ import annotations

import smtplib
import sqlite3
import ssl
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from coi.config import get_settings  # noqa: E402
from coi.emailer import send_email  # noqa: E402
from coi.llm import GroqJSONClient, LLMError  # noqa: E402


def check_sqlite() -> None:
    with tempfile.TemporaryDirectory() as d:
        conn = sqlite3.connect(Path(d) / "smoke.db")
        conn.execute("CREATE TABLE t (x TEXT)")
        conn.execute("INSERT INTO t VALUES ('ok')")
        assert conn.execute("SELECT x FROM t").fetchone()[0] == "ok"
        conn.close()
    print("[PASS] SQLite read/write")


def check_groq(settings) -> None:
    if not settings.groq_api_key:
        print("[SKIP] Groq — GROQ_API_KEY not set (use DEMO_MODE=true for the fallback engine)")
        return
    schema = {"type": "object", "properties": {"status": {"type": "string", "enum": ["OK"]},
                                               "echo": {"type": "string"}},
              "required": ["status", "echo"], "additionalProperties": False}
    try:
        data, _ = GroqJSONClient(settings).call_json(
            "Return structured output only, as JSON.", "Return status OK and echo the word 'smoke'.", schema)
        print(f"[PASS] Groq structured output via {settings.groq_model}: {data}")
    except LLMError as exc:
        print(f"[FAIL] Groq: {exc}")


def check_smtp(settings, send: bool) -> None:
    if not settings.smtp_configured:
        print("[SKIP] SMTP — SMTP_HOST not set (emails will be stored in the outbox as NOT_CONFIGURED)")
        return
    try:
        if settings.smtp_port == 465:
            server = smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, timeout=15, context=ssl.create_default_context())
        else:
            server = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=15)
            server.ehlo()
            if server.has_extn("starttls"):
                server.starttls(context=ssl.create_default_context())
                server.ehlo()
        if settings.smtp_username:
            server.login(settings.smtp_username, settings.smtp_password)
        server.quit()
        print(f"[PASS] SMTP connect/login to {settings.smtp_host}:{settings.smtp_port}")
    except Exception as exc:  # noqa: BLE001
        print(f"[FAIL] SMTP: {type(exc).__name__}: {exc}")
        return
    if send:
        from coi.db import init_db
        init_db(settings)
        res = send_email(settings, None, settings.reviewer_email, "SMTP_TEST", "[COI POC] SMTP test",
                         "<p>SMTP test from the COI POC.</p>", "SMTP test from the COI POC.")
        print(f"[INFO] Test email to {settings.reviewer_email}: {res['delivery_status']} {res['error'] or ''}")


if __name__ == "__main__":
    s = get_settings()
    check_sqlite()
    check_groq(s)
    check_smtp(s, "--send" in sys.argv)
    print("Deep links: open http://localhost:8501/?page=reviewer&case=COI-0001 after `streamlit run app.py`.")
