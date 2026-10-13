"""SMTP delivery: Gmail App Password handling and a real send to a local SMTP server (no external network)."""
import email
from email.header import decode_header, make_header
import socket
import threading
import time
import warnings

import pytest

from coi import db, demo, pipeline
from coi.config import Settings
from conftest import make_settings

with warnings.catch_warnings():
    warnings.simplefilter("ignore", DeprecationWarning)
    asyncore = pytest.importorskip("asyncore")
    smtpd = pytest.importorskip("smtpd")


def test_gmail_app_password_spaces_are_ignored():
    assert Settings(smtp_host="smtp.gmail.com", smtp_password="abcd efgh ijkl mnop").smtp_password == "abcdefghijklmnop"
    assert Settings(smtp_host="smtp.office365.com", smtp_password="pass with spaces").smtp_password == "pass with spaces"


class _CaptureServer(smtpd.SMTPServer):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.messages = []

    def process_message(self, peer, mailfrom, rcpttos, data, **kwargs):
        self.messages.append((mailfrom, rcpttos, data))


@pytest.fixture
def local_smtp():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = _CaptureServer(("127.0.0.1", port), None, decode_data=False)
    thread = threading.Thread(target=asyncore.loop, kwargs={"timeout": 0.05}, daemon=True)
    thread.start()
    yield server, port
    server.close()
    thread.join(timeout=2)


def test_reviewer_email_is_delivered_over_smtp(tmp_path, local_smtp):
    server, port = local_smtp
    s = make_settings(tmp_path, demo_mode=True, smtp_host="127.0.0.1", smtp_port=port, smtp_username="",
                      smtp_from_email="coi-poc@example.com", reviewer_email="reviewer@example.com")
    r = pipeline.submit_declaration(s, demo.scenario_context("GIBBERISH"), demo.scenario_answers("GIBBERISH"))
    for _ in range(40):
        if server.messages:
            break
        time.sleep(0.05)
    row = db.one(s, "SELECT * FROM emails WHERE case_id=?", (r.case_id,))
    assert row["delivery_status"] == "SENT", row["error"]
    (mailfrom, rcpttos, data), = server.messages
    msg = email.message_from_bytes(data)
    assert mailfrom == "coi-poc@example.com" and rcpttos == ["reviewer@example.com"]
    subject = str(make_header(decode_header(msg["Subject"])))  # MIME-encoded (en dash); mail clients decode it
    assert subject == f"[COI Information Required] {r.case_id} – Additional Information Required"
    assert msg.get_content_type() == "multipart/alternative"
    parts = {p.get_content_type(): p.get_payload(decode=True).decode("utf-8") for p in msg.walk() if not p.is_multipart()}
    assert "Open Declaration for Review" in parts["text/html"] and "asdfgh xyz 123 qwerty" in parts["text/plain"]
