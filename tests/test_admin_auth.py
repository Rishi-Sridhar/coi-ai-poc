"""Admin sign-in: hashing, lockout, and the page gate. Only the Admin page needs a login; every other page is open."""
import io
import re
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from coi import admin_auth
from ui import components

FAST = 2 ** 10          # tiny scrypt cost so the tests stay quick
PASSWORD = "correct horse battery"
ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def fresh_throttle(monkeypatch):
    monkeypatch.setattr(admin_auth, "THROTTLE", admin_auth.Throttle())


# ---- hashing -------------------------------------------------------------------------------------------------------

def test_hash_and_verify():
    h = admin_auth.hash_password(PASSWORD, n=FAST)
    assert admin_auth.is_configured(h)
    assert admin_auth.verify_password(PASSWORD, h)
    assert not admin_auth.verify_password("wrong password!!", h)
    assert not admin_auth.verify_password("", h)


def test_hash_is_salted_and_env_safe():
    a, b = admin_auth.hash_password(PASSWORD, n=FAST), admin_auth.hash_password(PASSWORD, n=FAST)
    assert a != b
    assert "$" not in a and " " not in a      # Compose/dotenv would expand '$'


@pytest.mark.parametrize("bad", ["", "   ", "plaintext", "scrypt:x:8:1:00:00", "bcrypt:1:8:1:00:00", "scrypt:1024:8:1:zz:zz"])
def test_malformed_hash_fails_closed(bad):
    assert not admin_auth.is_configured(bad)
    assert not admin_auth.verify_password(PASSWORD, bad)


def test_cli_hashes_stdin_and_enforces_min_length(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO(PASSWORD + "\n"))
    assert admin_auth._main(["--stdin"]) == 0
    line = capsys.readouterr().out.strip()
    assert line.startswith("ADMIN_PASSWORD_HASH=")
    assert admin_auth.verify_password(PASSWORD, line.split("=", 1)[1])
    monkeypatch.setattr("sys.stdin", io.StringIO("short\n"))
    assert admin_auth._main(["--stdin"]) == 1


# ---- lockout -------------------------------------------------------------------------------------------------------

def test_lockout_after_max_failures_then_expires():
    t = admin_auth.Throttle()
    for i in range(admin_auth.MAX_FAILURES - 1):
        t.record_failure(1000 + i)
    assert t.locked_for(1010) == 0
    t.record_failure(1010)
    assert t.locked_for(1011) > 0
    assert t.locked_for(1010 + admin_auth.LOCKOUT_SECONDS + 1) == 0


def test_old_failures_expire_and_success_resets():
    t = admin_auth.Throttle()
    for i in range(admin_auth.MAX_FAILURES - 1):
        t.record_failure(i)
    t.record_failure(admin_auth.FAILURE_WINDOW + 100)      # earlier ones fell out of the window
    assert t.locked_for(admin_auth.FAILURE_WINDOW + 101) == 0
    for i in range(admin_auth.MAX_FAILURES - 1):
        t.record_failure(5000 + i)
    t.record_success()
    t.record_failure(5100)
    assert t.locked_for(5101) == 0


# ---- the page gate -------------------------------------------------------------------------------------------------

def _admin_script():
    from coi.config import get_settings
    from coi.db import init_db
    from ui import admin_page
    s = get_settings()
    init_db(s)
    admin_page.render(s)


def _open_admin(monkeypatch, tmp_path, hash_value):
    monkeypatch.setenv("COI_DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("COI_UPLOADS_DIR", str(tmp_path / "uploads"))
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", hash_value)
    return AppTest.from_function(_admin_script, default_timeout=30).run()


def _labels(at):
    return [b.label for b in at.button]


def _sign_in(at, password):
    at.text_input[0].input(password)
    next(b for b in at.button if b.label == "Sign in").click()
    return at.run()


def test_locked_when_no_hash_configured(monkeypatch, tmp_path):
    at = _open_admin(monkeypatch, tmp_path, "")
    assert any("not configured" in w.value for w in at.warning)
    assert "Reset demo data" not in _labels(at) and not at.text_input


def test_sign_in_form_shown_and_nothing_sensitive_rendered(monkeypatch, tmp_path):
    at = _open_admin(monkeypatch, tmp_path, admin_auth.hash_password(PASSWORD, n=FAST))
    assert [t.label for t in at.text_input] == ["Admin password"]
    assert _labels(at) == ["Sign in"]
    assert not at.toggle and not at.checkbox and not at.tabs


def test_wrong_password_rejected(monkeypatch, tmp_path):
    at = _open_admin(monkeypatch, tmp_path, admin_auth.hash_password(PASSWORD, n=FAST))
    at = _sign_in(at, "not the password")
    assert any("Incorrect" in e.value for e in at.error)
    assert "Reset demo data" not in _labels(at)


def test_correct_password_unlocks_and_sign_out_relocks(monkeypatch, tmp_path):
    at = _open_admin(monkeypatch, tmp_path, admin_auth.hash_password(PASSWORD, n=FAST))
    at = _sign_in(at, PASSWORD)
    assert "Reset demo data" in _labels(at) and "Sign out" in _labels(at)
    next(b for b in at.button if b.label == "Sign out").click()
    at.run()
    assert "Reset demo data" not in _labels(at) and _labels(at) == ["Sign in"]


def test_lockout_blocks_even_the_correct_password(monkeypatch, tmp_path):
    at = _open_admin(monkeypatch, tmp_path, admin_auth.hash_password(PASSWORD, n=FAST))
    for _ in range(admin_auth.MAX_FAILURES):
        at = _sign_in(at, "wrong wrong wrong")
    at = _sign_in(at, PASSWORD)
    assert any("Too many" in e.value for e in at.error)
    assert "Reset demo data" not in _labels(at)


def test_expired_session_must_sign_in_again(monkeypatch, tmp_path):
    at = _open_admin(monkeypatch, tmp_path, admin_auth.hash_password(PASSWORD, n=FAST))
    at = _sign_in(at, PASSWORD)
    assert "Reset demo data" in _labels(at)
    at.session_state["_admin_until"] = 1.0                  # long ago
    at.run()
    assert "Reset demo data" not in _labels(at) and _labels(at) == ["Sign in"]


# ---- everything else stays open ------------------------------------------------------------------------------------

def test_outbox_needs_no_sign_in(monkeypatch, tmp_path):
    def script():
        from coi.config import get_settings
        from coi.db import init_db
        from ui import outbox_page
        s = get_settings()
        init_db(s)
        outbox_page.render(s)
    monkeypatch.setenv("COI_DB_PATH", str(tmp_path / "o.db"))
    monkeypatch.setenv("COI_UPLOADS_DIR", str(tmp_path / "uploads"))
    at = AppTest.from_function(script, default_timeout=30).run()
    assert not at.exception and not at.text_input
    assert any("No emails yet" in i.value for i in at.info)


def test_page_registry_splits_outbox_from_admin():
    assert "outbox" in components.PAGES and "admin" in components.PAGES
    assert {"outbox", "admin"} <= set(components.MORE_PAGES)
    assert "outbox" not in components.PRIMARY_PAGES


def test_sensitive_actions_exist_only_in_the_admin_page():
    """Guardrail: no other UI module may call the destructive/sensitive functions."""
    sensitive = re.compile(r"\b(reset_demo|set_setting|send_email)\s*\(")
    offenders = [p.name for p in (ROOT / "ui").glob("*.py") if p.name != "admin_page.py" and sensitive.search(p.read_text(encoding="utf-8"))]
    assert offenders == []
    assert "admin_auth" not in (ROOT / "app.py").read_text(encoding="utf-8")   # gate lives in the page, not scattered
