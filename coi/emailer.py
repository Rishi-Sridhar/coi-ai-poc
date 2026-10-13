"""Reviewer / respondent emails. Outbox-first: every email is written to SQLite BEFORE any SMTP attempt.

Delivery status: SENT | FAILED | NOT_CONFIGURED. Email failure never loses the case or raises.
"""
from __future__ import annotations

import smtplib
import ssl
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime
from html import escape

from .actions import action_label, display_actions, recommended_actions
from .config import BANNER, Settings
from .db import audit, connect, latest_submission, utcnow
from .display import VERDICT_DISPLAY, concise_summary, humanize, key_items, next_action, question_title, referenced_ids
from .guardrails import VERDICT_INSUFFICIENT, VERDICT_POTENTIAL, VERDICT_UNAVAILABLE

ENGINE_LABEL_LLM = "AI preliminary assessment"
ENGINE_LABEL_FALLBACK = "Demo fallback assessment (deterministic rules — not an LLM output)"


def review_link(settings: Settings, case_id: str) -> str:
    return f"{settings.app_base_url}/?page=reviewer&case={case_id}"


def resubmit_link(settings: Settings, case_id: str) -> str:
    return f"{settings.app_base_url}/?page=submit&resubmit={case_id}"


def reviewer_subject(case_id: str, verdict: str, routing_priority: str) -> str:
    if verdict == VERDICT_INSUFFICIENT:
        return f"[COI Information Required] {case_id} – Additional Information Required"
    if verdict == VERDICT_POTENTIAL:
        return f"[COI Review Required] {case_id} – Potential Conflict Identified"
    if verdict == VERDICT_UNAVAILABLE:
        return f"[COI Review Required] {case_id} – Manual Review Required"
    if routing_priority == "CONFIRMATION":
        return f"[COI Confirmation Requested] {case_id} – No Conflict Apparent"
    return f"[COI Review Required] {case_id} – No Conflict Apparent"


# ---------------------------------------------------------------- business email layout (inline styles for email clients)

_FONT = "font-family:Segoe UI,Arial,Helvetica,sans-serif"
_TONES = {"danger": ("#b42318", "#fef3f2"), "ok": ("#067647", "#ecfdf3"), "warn": ("#b54708", "#fffaeb"),
          "neutral": ("#667085", "#f2f4f7")}


def _fmt_time(iso: str | None) -> str:
    if not iso:
        return ""
    try:
        return datetime.fromisoformat(iso).astimezone().strftime("%d %b %Y, %H:%M")
    except ValueError:
        return iso


def _section(title: str) -> str:
    return (f'<tr><td style="padding:22px 28px 6px 28px;{_FONT};font-size:12px;font-weight:700;letter-spacing:.08em;'
            f'text-transform:uppercase;color:#475467;border-bottom:1px solid #eaecf0">{escape(title)}</td></tr>')


def _row(html: str, pad: str = "10px 28px 0 28px") -> str:
    return f'<tr><td style="padding:{pad};{_FONT};font-size:14px;color:#101828;line-height:1.5">{html}</td></tr>'


def _answer_block(qid: str, value: str) -> str:
    return (f'<div style="margin:8px 0 0 0"><div style="font-size:13px;color:#475467">{escape(question_title(qid))}</div>'
            f'<div style="font-size:15px;font-weight:600;color:#101828;white-space:pre-wrap">{escape(value)}</div></div>')


def _quote(text: str) -> str:
    return (f'<div style="margin:4px 0 8px 0;padding:6px 10px;border-left:3px solid #fdb022;background:#ffffff;'
            f'font-style:italic;white-space:pre-wrap">“{escape(text)}”</div>')


def _button(link: str, label_: str) -> str:
    return (f'<table role="presentation" cellpadding="0" cellspacing="0" style="margin:6px 0"><tr>'
            f'<td style="background:#1d2939;border-radius:6px">'
            f'<a href="{escape(link)}" target="_blank" rel="noopener" style="display:inline-block;padding:11px 22px;'
            f'{_FONT};font-size:15px;font-weight:600;color:#ffffff;text-decoration:none">{escape(label_)}</a>'
            f'</td></tr></table>')


def _link_line(link: str) -> str:
    return (f'<div style="font-size:12px;color:#667085;margin-top:4px">or paste this link into your browser: '
            f'<a href="{escape(link)}" target="_blank" rel="noopener" style="color:#475467">{escape(link)}</a></div>')


def _frame(title: str, case_id: str, rows_html: list[str], footer: str) -> str:
    return (f'<div style="background:#f5f6f8;padding:24px 0">'
            f'<table role="presentation" cellpadding="0" cellspacing="0" width="100%" style="max-width:680px;margin:0 auto;'
            f'background:#ffffff;border:1px solid #eaecf0;border-radius:8px;border-collapse:separate">'
            f'<tr><td style="background:#1d2939;border-radius:8px 8px 0 0;padding:16px 28px;{_FONT}">'
            f'<div style="color:#ffffff;font-size:17px;font-weight:600">{escape(title)}</div>'
            f'<div style="color:#d0d5dd;font-size:13px;margin-top:2px">Case {escape(case_id)}</div></td></tr>'
            + "".join(rows_html)
            + f'<tr><td style="padding:22px 28px 24px 28px;{_FONT};font-size:12px;color:#667085;line-height:1.5;'
              f'border-top:1px solid #eaecf0">{footer}</td></tr></table></div>')


def _contradiction_others(issue: dict, answers: dict) -> list[str]:
    return [r for r in referenced_ids(issue.get("explanation")) if r != issue["question_id"] and r in answers]


def build_reviewer_email(settings: Settings, case: dict, assessment: dict, answers: dict) -> tuple[str, str, str]:
    """Business-style reviewer notification.

    assessment: {verdict, engine_label, routing, routing_priority, result, quality_issues}. Uses only persisted data:
    exact submitted answers, validated AI citations and quality findings. No internal IDs, routing rules,
    precedent mechanics or AI implementation details.
    """
    cid = case["case_id"]
    verdict = assessment["verdict"]
    result = assessment.get("result") or {}
    subject = reviewer_subject(cid, verdict, assessment["routing_priority"])
    link = review_link(settings, cid)
    title, tone = VERDICT_DISPLAY.get(verdict, (verdict.title(), "neutral"))
    border, bg = _TONES[tone]
    if verdict == VERDICT_UNAVAILABLE:
        reason = "The automated assessment could not be completed, so the declaration requires manual review."
    else:
        reason, _ = concise_summary(humanize(result.get("overall_explanation", "")))
    sub = latest_submission(settings, cid)
    submitted = _fmt_time(sub["created_at"] if sub else case.get("created_at"))
    owner = " · Opportunity Owner" if case.get("is_owner") == "Yes" else ""
    a_view = {"verdict": verdict, "result": result}
    heading, items = key_items(a_view, answers)
    issues = assessment.get("quality_issues", [])
    step_title, step_text = next_action(case, a_view, None)
    intro = ("A Conflict of Interest declaration requires your confirmation."
             if assessment["routing_priority"] == "CONFIRMATION" else
             "A Conflict of Interest declaration requires your review.")
    demo_note = (" Demo Mode: this preliminary assessment was produced by the rules-based demo fallback, not by AI."
                 if assessment.get("engine_label") == ENGINE_LABEL_FALLBACK else "")

    details = [("Respondent", f"{case['respondent_name']} — {case['respondent_role']}{owner}"),
               ("Opportunity", f"{case['opportunity_id']} — {case['opportunity_name']}"),
               ("Client", case["client"]),
               ("State-Owned Entity", f"{case['soe']} ({case['soe_relationship']})"),
               ("Submitted", submitted)]
    detail_html = "".join(f'<tr><td style="padding:3px 16px 3px 0;color:#667085;white-space:nowrap;vertical-align:top">'
                          f'{escape(k)}</td><td style="padding:3px 0;color:#101828">{escape(v)}</td></tr>'
                          for k, v in details)

    rows_html = [
        _row(escape(intro), "20px 28px 0 28px"),
        _row(f'<div style="border-left:5px solid {border};background:{bg};border-radius:6px;padding:14px 18px">'
             f'<div style="font-size:11px;font-weight:700;letter-spacing:.1em;text-transform:uppercase;color:#475467">'
             f'Preliminary assessment</div><div style="font-size:20px;font-weight:700;color:{border};margin-top:2px">'
             f'{escape(title)}</div><div style="font-size:14px;color:#101828;margin-top:6px">{escape(reason)}</div></div>',
             "14px 28px 0 28px"),
        _row(f'<table role="presentation" cellpadding="0" cellspacing="0" style="{_FONT};font-size:14px">'
             f'{detail_html}</table>', "16px 28px 0 28px"),
    ]
    if items:
        rows_html += [_section(heading), _row("".join(_answer_block(i["qid"], i["value"]) for i in items))]

    if issues:
        only_contradiction = all(i["issue_type"] == "CONTRADICTORY" for i in issues)
        rows_html.append(_section("Inconsistent information" if only_contradiction else "Additional information required"))
        for i in issues:
            qid = i["question_id"]
            if i["issue_type"] == "CONTRADICTORY":
                body = ('<div style="font-weight:600;color:#93370d">Inconsistent information identified</div>'
                        + "".join(f'<div style="margin-top:4px">{escape(question_title(r))}: <b>{escape(answers[r])}</b></div>'
                                  for r in _contradiction_others(i, answers))
                        + f'<div style="margin-top:6px;color:#475467">However, the respondent also stated '
                          f'({escape(question_title(qid))}):</div>' + _quote(i["submitted_text"]))
            else:
                body = (f'<div style="font-weight:600;color:#93370d">{escape(question_title(qid))}</div>'
                        f'<div style="margin-top:4px;color:#475467">Submitted:</div>' + _quote(i["submitted_text"])
                        + f'<div><b>Why this needs attention:</b> {escape(humanize(i["explanation"]))}</div>')
            body += (f'<div style="margin-top:4px"><b>Requested clarification:</b> '
                     f'{escape(humanize(i["follow_up_question"]))}</div>')
            rows_html.append(_row(f'<div style="border:1px solid #fedf89;background:#fffcf5;border-radius:6px;'
                                  f'padding:10px 14px">{body}</div>'))

    # Workflow steps (Escalate to Compliance) are not listed as recommendations; the next action covers routing.
    actions = display_actions(recommended_actions({"routing": assessment.get("routing"), "result": result}))
    if actions:
        first, also = actions[0], actions[1:]
        body = (f'<div style="font-size:16px;font-weight:700;color:#101828">{escape(action_label(first["action_code"]))}</div>'
                f'<div style="margin-top:2px">{escape(humanize(first.get("reason", "")))}</div>')
        for x in also:
            body += (f'<div style="margin-top:8px"><span style="color:#475467">Also:</span> '
                     f'<b>{escape(action_label(x["action_code"]))}</b> — {escape(humanize(x.get("reason", "")))}</div>')
        body += ('<div style="margin-top:8px;font-size:12px;color:#667085">The final mitigation decision remains with '
                 'the Compliance reviewer.</div>')
        rows_html += [_section("Recommended action"), _row(body)]

    rows_html += [_section("Next action"),
                  _row(f"<b>{escape(step_title)}</b> — {escape(step_text)}"),
                  _row(_button(link, "Open Declaration for Review") + _link_line(link), "14px 28px 0 28px")]
    footer = (f"This is a preliminary assessment to support your review. The final compliance decision rests with the "
              f"reviewer.{escape(demo_note)}<br>{escape(BANNER)}")
    html = _frame("Conflict of Interest — Review Request", cid, rows_html, footer)

    t = [f"CONFLICT OF INTEREST — REVIEW REQUEST ({cid})", "", intro, "",
         f"PRELIMINARY ASSESSMENT: {title}", reason, ""]
    t += [f"{k}: {v}" for k, v in details]
    if items:
        t += ["", heading.upper()] + [f"  {question_title(i['qid'])}\n    {i['value']}" for i in items]
    if issues:
        t += ["", "ADDITIONAL INFORMATION REQUIRED"]
        for i in issues:
            qid = i["question_id"]
            if i["issue_type"] == "CONTRADICTORY":
                t += ["  Inconsistent information identified"]
                t += [f"    {question_title(r)}: {answers[r]}" for r in _contradiction_others(i, answers)]
                t += [f"    However, the respondent also stated ({question_title(qid)}):", f"    \"{i['submitted_text']}\""]
            else:
                t += [f"  {question_title(qid)}", f"    Submitted: \"{i['submitted_text']}\"",
                      f"    Why this needs attention: {humanize(i['explanation'])}"]
            t += [f"    Requested clarification: {humanize(i['follow_up_question'])}"]
    if actions:
        t += ["", "RECOMMENDED ACTION", f"  {action_label(actions[0]['action_code'])}",
              f"    {humanize(actions[0].get('reason', ''))}"]
        t += [f"  Also: {action_label(x['action_code'])} — {humanize(x.get('reason', ''))}" for x in actions[1:]]
        t += ["  The final mitigation decision remains with the Compliance reviewer."]
    t += ["", "NEXT ACTION", f"  {step_title} — {step_text}", "", "OPEN DECLARATION FOR REVIEW", f"  {link}", "",
          "This is a preliminary assessment to support your review. The final compliance decision rests with the "
          "reviewer." + demo_note, BANNER]
    return subject, html, "\n".join(t)


def build_info_request_email(settings: Settings, case: dict, fields: list[str], answers: dict, message: str,
                             reviewer: str) -> tuple[str, str, str]:
    cid = case["case_id"]
    subject = f"[COI Information Requested] {cid} – Additional information required"
    link = resubmit_link(settings, cid)
    rows_html = [
        _row(f"Dear {escape(case['respondent_name'])},", "20px 28px 0 28px"),
        _row(f"The compliance reviewer needs more information about your Conflict of Interest declaration for "
             f"<b>{escape(case['opportunity_name'])}</b> before it can be completed."),
        _section("Message from the reviewer"),
        _row(f'<div style="white-space:pre-wrap">{escape(message)}</div>'),
        _section("Questions to clarify"),
        _row("".join(_answer_block(f, answers.get(f, "(not answered)")) for f in fields)),
        _section("Update your declaration"),
        _row(_button(link, "Update Declaration") + _link_line(link), "14px 28px 0 28px"),
    ]
    html = _frame("Conflict of Interest — Information Request", cid, rows_html,
                  f"Requested by {escape(reviewer)}.<br>{escape(BANNER)}")
    text = "\n".join([f"CONFLICT OF INTEREST — INFORMATION REQUEST ({cid})", "",
                      f"Dear {case['respondent_name']},", "",
                      f"The compliance reviewer needs more information about your declaration for "
                      f"{case['opportunity_name']}.", "", "MESSAGE FROM THE REVIEWER", f"  {message}", "",
                      "QUESTIONS TO CLARIFY"]
                     + [f"  {question_title(f)}: current answer \"{answers.get(f, '(not answered)')}\"" for f in fields]
                     + ["", "UPDATE YOUR DECLARATION", f"  {link}", "", f"Requested by {reviewer}.", BANNER])
    return subject, html, text


def _smtp_send(settings: Settings, recipient: str, subject: str, html: str, text: str) -> None:
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = settings.smtp_from_email or settings.smtp_username
    msg["To"] = recipient
    msg.attach(MIMEText(text, "plain", "utf-8"))
    msg.attach(MIMEText(html, "html", "utf-8"))
    if settings.smtp_port == 465:
        with smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, timeout=15,
                              context=ssl.create_default_context()) as server:
            if settings.smtp_username:
                server.login(settings.smtp_username, settings.smtp_password)
            server.send_message(msg)
    else:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=15) as server:
            server.ehlo()
            if server.has_extn("starttls"):
                server.starttls(context=ssl.create_default_context())
                server.ehlo()
            if settings.smtp_username:
                server.login(settings.smtp_username, settings.smtp_password)
            server.send_message(msg)


def send_email(settings: Settings, case_id: str | None, recipient: str, kind: str,
               subject: str, html: str, text: str) -> dict:
    """1) write to outbox  2) attempt SMTP  3) record status. Never raises."""
    with connect(settings) as conn:
        cur = conn.execute(
            "INSERT INTO emails (case_id, recipient, kind, subject, body_html, body_text, delivery_status, error, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (case_id, recipient, kind, subject, html, text, "QUEUED", None, utcnow()),
        )
        email_id = cur.lastrowid
    audit(settings, case_id, "EMAIL_QUEUED", "system", {"email_id": email_id, "kind": kind, "recipient": recipient, "subject": subject})

    if not settings.smtp_configured:
        status, error = "NOT_CONFIGURED", "SMTP_HOST not set — email kept in outbox only."
    else:
        try:
            _smtp_send(settings, recipient, subject, html, text)
            status, error = "SENT", None
        except Exception as exc:  # noqa: BLE001 — email failure must never crash the app
            status, error = "FAILED", f"{type(exc).__name__}: {exc}"
    with connect(settings) as conn:
        conn.execute("UPDATE emails SET delivery_status=?, error=? WHERE id=?", (status, error, email_id))
    return {"email_id": email_id, "delivery_status": status, "error": error}
