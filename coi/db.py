"""SQLite persistence: schema, seeding, reset and small query helpers.

Rows are insert-only where practical. The exceptions are cases.status/updated_at,
emails.delivery_status/error (outbox-first: written, then updated after the SMTP attempt)
and precedents.active (retire / reactivate).
"""
from __future__ import annotations

import json
import shutil
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

import yaml

from .config import CONFIG_DIR, Settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS cases (
    case_id TEXT PRIMARY KEY,
    opportunity_id TEXT, opportunity_name TEXT, client TEXT, soe TEXT,
    soe_relationship TEXT, opportunity_description TEXT,
    respondent_name TEXT, respondent_email TEXT, respondent_role TEXT,
    is_owner TEXT,
    status TEXT NOT NULL,
    routing_priority TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS submissions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id TEXT NOT NULL REFERENCES cases(case_id),
    version INTEGER NOT NULL,
    answers_json TEXT NOT NULL,
    rule_results_json TEXT NOT NULL,
    questionnaire_version TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS assessments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id TEXT NOT NULL REFERENCES cases(case_id),
    submission_id INTEGER REFERENCES submissions(id),
    state TEXT NOT NULL,
    engine TEXT,
    quality_json TEXT,
    result_json TEXT,
    verdict TEXT NOT NULL,
    routing_json TEXT,
    precedents_json TEXT,
    guardrails_json TEXT,
    model TEXT, prompt_version TEXT, guidance_version TEXT,
    rendered_prompt TEXT, raw_response TEXT, error TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id TEXT NOT NULL REFERENCES cases(case_id),
    assessment_id INTEGER REFERENCES assessments(id),
    action TEXT NOT NULL,
    final_outcome TEXT,
    agrees_with_ai INTEGER,
    rationale TEXT,
    info_request_json TEXT,
    use_as_precedent INTEGER NOT NULL DEFAULT 0,
    mitigation_notes TEXT,
    additional_comments TEXT,
    reviewer_name TEXT,
    created_at TEXT NOT NULL,
    final_action_codes TEXT,
    final_action_notes TEXT
);
CREATE TABLE IF NOT EXISTS precedents (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    case_id TEXT,
    decision_id INTEGER,
    snapshot_json TEXT NOT NULL,
    outcome TEXT NOT NULL,
    rationale TEXT NOT NULL,
    guidance_version TEXT,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    retired_at TEXT,
    retired_reason TEXT
);
CREATE TABLE IF NOT EXISTS emails (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id TEXT,
    recipient TEXT NOT NULL,
    kind TEXT NOT NULL,
    subject TEXT NOT NULL,
    body_html TEXT, body_text TEXT,
    delivery_status TEXT NOT NULL,
    error TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id TEXT,
    event_type TEXT NOT NULL,
    actor TEXT,
    details_json TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS app_settings (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def connect(settings: Settings) -> Iterator[sqlite3.Connection]:
    Path(settings.db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(settings.db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db(settings: Settings) -> None:
    with connect(settings) as conn:
        conn.executescript(SCHEMA)
        # Backwards-compatible migration for databases created before final actions existed.
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(decisions)").fetchall()}
        for col in ("final_action_codes", "final_action_notes"):
            if col not in cols:
                conn.execute(f"ALTER TABLE decisions ADD COLUMN {col} TEXT")
        count = conn.execute("SELECT COUNT(*) FROM precedents WHERE source='SYNTHETIC'").fetchone()[0]
    if count == 0:
        seed_synthetic_precedents(settings)


def load_synthetic_seeds() -> list[dict]:
    data = yaml.safe_load((CONFIG_DIR / "synthetic_precedents.yaml").read_text(encoding="utf-8"))
    return data["precedents"]


def seed_synthetic_precedents(settings: Settings) -> None:
    with connect(settings) as conn:
        conn.execute("DELETE FROM precedents WHERE source='SYNTHETIC'")
        for seed in load_synthetic_seeds():
            snapshot = {
                "title": seed["title"],
                "opportunity_summary": seed["opportunity_summary"],
                "is_owner": seed["is_owner"],
                "soe_relationship": seed["soe_relationship"],
                "answers": {k: str(v) for k, v in seed["answers"].items()},
                "final_actions": list(seed.get("final_actions") or []),
            }
            conn.execute(
                "INSERT INTO precedents (id, source, case_id, decision_id, snapshot_json, outcome, rationale,"
                " guidance_version, active, created_at) VALUES (?,?,?,?,?,?,?,?,1,?)",
                (seed["id"], "SYNTHETIC", None, None, json.dumps(snapshot), seed["outcome"],
                 seed["rationale"], settings.guidance_version, seed["created_at"]),
            )


def reset_demo(settings: Settings) -> None:
    """Remove demo-created data; restore original synthetic seeds; case numbering restarts at COI-0001."""
    with connect(settings) as conn:
        for table in ("audit_events", "emails", "decisions", "assessments", "submissions", "cases", "app_settings"):
            conn.execute(f"DELETE FROM {table}")
        conn.execute("DELETE FROM precedents WHERE source='REVIEWER'")
        conn.execute("DELETE FROM sqlite_sequence")
    seed_synthetic_precedents(settings)
    uploads = Path(settings.uploads_dir)
    if uploads.exists():
        for child in uploads.iterdir():
            if child.is_dir():
                shutil.rmtree(child, ignore_errors=True)


# ---------------------------------------------------------------- helpers

def audit(settings: Settings, case_id: str | None, event_type: str, actor: str, details: dict | None = None) -> None:
    with connect(settings) as conn:
        conn.execute(
            "INSERT INTO audit_events (case_id, event_type, actor, details_json, created_at) VALUES (?,?,?,?,?)",
            (case_id, event_type, actor, json.dumps(details or {}), utcnow()),
        )


def next_case_id(conn: sqlite3.Connection) -> str:
    row = conn.execute("SELECT case_id FROM cases ORDER BY case_id DESC LIMIT 1").fetchone()
    n = int(row["case_id"].split("-")[1]) + 1 if row else 1
    return f"COI-{n:04d}"


def next_reviewer_precedent_id(conn: sqlite3.Connection) -> str:
    row = conn.execute("SELECT id FROM precedents WHERE source='REVIEWER' ORDER BY id DESC LIMIT 1").fetchone()
    n = int(row["id"].split("-")[1]) + 1 if row else 1
    return f"REV-{n:03d}"


def get_setting(settings: Settings, key: str, default: str = "") -> str:
    with connect(settings) as conn:
        row = conn.execute("SELECT value FROM app_settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(settings: Settings, key: str, value: str) -> None:
    with connect(settings) as conn:
        conn.execute("INSERT OR REPLACE INTO app_settings (key, value) VALUES (?,?)", (key, value))


def simulate_ai_failure(settings: Settings) -> bool:
    return get_setting(settings, "simulate_ai_failure", "false") == "true"


def rows(settings: Settings, sql: str, params: tuple = ()) -> list[dict]:
    with connect(settings) as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def one(settings: Settings, sql: str, params: tuple = ()) -> dict | None:
    result = rows(settings, sql, params)
    return result[0] if result else None


def get_case(settings: Settings, case_id: str) -> dict | None:
    return one(settings, "SELECT * FROM cases WHERE case_id=?", (case_id,))


def latest_submission(settings: Settings, case_id: str) -> dict | None:
    sub = one(settings, "SELECT * FROM submissions WHERE case_id=? ORDER BY version DESC LIMIT 1", (case_id,))
    if sub:
        sub["answers"] = json.loads(sub["answers_json"])
        sub["rule_results"] = json.loads(sub["rule_results_json"])
    return sub


def latest_assessment(settings: Settings, case_id: str) -> dict | None:
    a = one(settings, "SELECT * FROM assessments WHERE case_id=? ORDER BY id DESC LIMIT 1", (case_id,))
    if a:
        for col in ("quality_json", "result_json", "routing_json", "precedents_json", "guardrails_json"):
            a[col.replace("_json", "")] = json.loads(a[col]) if a[col] else None
    return a


def list_precedents(settings: Settings, active_only: bool = False) -> list[dict]:
    sql = "SELECT * FROM precedents" + (" WHERE active=1" if active_only else "") + " ORDER BY created_at"
    out = rows(settings, sql)
    for p in out:
        p["snapshot"] = json.loads(p["snapshot_json"])
    return out
