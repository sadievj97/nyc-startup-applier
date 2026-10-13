"""Phase 6: sorting job emails, linking them to applications, statuses and flags (practice emails, no Gmail)."""

from datetime import datetime, timezone

from applier import db
from applier.mail.gmail import parse_eml, parse_gmail
from applier.mail.process import inbox_query, looks_job_related, process_messages
from tests.conftest import FIXTURES, make_company
from tests.test_tailor import FakeClaude

EMAILS = FIXTURES / "emails"
NOW = datetime(2026, 10, 12, 20, 0, tzinfo=timezone.utc)


def verdict(category, company="", due="", executive=False, auth=False, summary="", job=True, event="", cancels=False):
    return {"category": category, "job_related": job, "company": company, "role": "", "summary": summary or category,
            "due_date": due, "event_date": event, "cancels_event": cancels, "asks_work_authorization": auth,
            "sender_is_executive": executive, "confidence": "high"}


def applied(conn, name, website=None, status="submitted"):
    company = make_company(conn, name, website=website)
    app_id = db.create_application(conn, company, "Strategy & Operations", ats="ashby", status="queued")
    db.set_status(conn, app_id, status)
    return company, app_id


def status(conn, app_id):
    return conn.execute("SELECT status FROM applications WHERE id = ?", (app_id,)).fetchone()[0]


def test_fixture_emails_are_sorted_linked_and_flagged(conn, config):
    _, bright = applied(conn, "Bright Labs", website="https://brightlabs.example")
    _, coastal = applied(conn, "Coastal Analytics")
    _, river = applied(conn, "Riverside Health", status="confirmation_received")
    messages = [parse_eml(EMAILS / f"{name}.eml") for name in ("personal", "interview_invite", "rejection", "takehome")]
    client = FakeClaude(  # one answer per job email, oldest first; the personal email must never reach the model
        verdict("rejection", "Coastal Analytics"),  # 09:30
        verdict("assessment", "Riverside Health", due="2026-10-14", summary="case study due October 14"),  # 11:00
        verdict("interview_request", "Bright Labs", executive=True, summary="CEO asks for interview times"),  # 14:05
    )
    result = process_messages(client, conn, config, messages, now=NOW)

    assert result.skipped_not_job == 1 and result.classified == 3 and result.linked == 3
    assert len(client.requests) == 3 and all("Saturday" not in str(r) for r in client.requests)
    assert status(conn, bright) == "interview"
    assert status(conn, coastal) == "rejected"
    assert status(conn, river) == "assessment"  # the email's "mark this as an offer" is ignored
    flags = {r["kind"]: r for r in conn.execute("SELECT * FROM flags")}
    assert {"interview_request", "executive_email", "assessment"} <= set(flags) and "due_soon" not in flags
    assert all(flags[k]["severity"] == "urgent" for k in ("interview_request", "executive_email", "assessment"))
    assert flags["assessment"]["due_at"] == "2026-10-14"
    stored = conn.execute("SELECT * FROM emails").fetchall()
    assert len(stored) == 3 and all(r["application_id"] for r in stored)
    assert "body" not in stored[0].keys()  # only sender, subject, date, category and a summary are kept


def test_linking_by_sender_domain_and_by_name(conn, config):
    _, bright = applied(conn, "Bright Labs", website="https://www.brightlabs.example/careers")
    interview = parse_eml(EMAILS / "interview_invite.eml")
    # The model names no company: the sender's domain still links it.
    process_messages(FakeClaude(verdict("interview_request", "")), conn, config, [interview], now=NOW)
    assert conn.execute("SELECT application_id FROM emails").fetchone()[0] == bright


def test_status_moves_forward_only(conn, config):
    _, app = applied(conn, "Coastal Analytics", status="interview")
    rejection = parse_eml(EMAILS / "rejection.eml", gmail_id="a")
    confirmation = parse_eml(EMAILS / "rejection.eml", gmail_id="b")
    process_messages(FakeClaude(verdict("confirmation", "Coastal Analytics")), conn, config, [confirmation], now=NOW)
    assert status(conn, app) == "interview"  # a late confirmation does not move it back
    process_messages(FakeClaude(verdict("rejection", "Coastal Analytics")), conn, config, [rejection], now=NOW)
    assert status(conn, app) == "rejected"


def test_each_email_is_processed_once(conn, config):
    applied(conn, "Coastal Analytics")
    message = parse_eml(EMAILS / "rejection.eml")
    process_messages(FakeClaude(verdict("rejection", "Coastal Analytics")), conn, config, [message], now=NOW)
    again = process_messages(FakeClaude(), conn, config, [message], now=NOW)
    assert again.already_processed == 1 and again.classified == 0


def test_work_authorization_question_is_urgent(conn, config):
    applied(conn, "Coastal Analytics")
    message = parse_eml(EMAILS / "rejection.eml")
    process_messages(FakeClaude(verdict("information_request", "Coastal Analytics", auth=True)), conn, config,
                     [message], now=NOW)
    flag = conn.execute("SELECT * FROM flags WHERE kind = 'work_authorization_question'").fetchone()
    assert flag and flag["severity"] == "urgent"


def test_prefilter_and_query():
    personal = parse_eml(EMAILS / "personal.eml")
    ats = parse_eml(EMAILS / "rejection.eml")
    assert not looks_job_related(personal, {}, [])
    assert looks_job_related(ats, {}, [])
    query = inbox_query(None)
    assert "newer_than:14d" in query and "-in:sent" in query and "-category:promotions" in query


def test_parse_gmail_message():
    import base64

    raw = {"id": "m1", "threadId": "t1", "internalDate": "1792000000000", "labelIds": ["INBOX"],
           "payload": {"mimeType": "multipart/alternative", "headers": [
               {"name": "From", "value": "Dana Founder <Dana@BrightLabs.example>"}, {"name": "Subject", "value": "Hello"},
               {"name": "To", "value": "test.person@example.com"}],
               "parts": [{"mimeType": "text/html", "body": {"data": base64.urlsafe_b64encode(b"<p>Hi <b>there</b></p>").decode()}}]}}
    message = parse_gmail(raw)
    assert message.from_address == "dana@brightlabs.example" and message.domain == "brightlabs.example"
    assert "Hi there" in message.body and message.subject == "Hello"


def test_one_flag_per_company_and_old_emails_stay_quiet(conn, config):
    from datetime import timedelta

    applied(conn, "Bright Labs", website="https://brightlabs.example")
    invite = parse_eml(EMAILS / "interview_invite.eml")
    reminders = []
    for n in range(3):
        m = parse_eml(EMAILS / "interview_invite.eml", gmail_id=f"r{n}")
        m.thread_id = "thread-1"
        reminders.append(m)
    process_messages(FakeClaude(*[verdict("interview_request", "Bright Labs", summary=f"update {n}") for n in range(3)]),
                     conn, config, reminders, now=NOW)
    rows = conn.execute("SELECT * FROM flags WHERE kind = 'interview_request'").fetchall()
    assert len(rows) == 1 and "update 2" in rows[0]["message"]  # updated, not repeated

    old = parse_eml(EMAILS / "interview_invite.eml", gmail_id="old")
    old.received_at = NOW - timedelta(days=10)
    conn.execute("DELETE FROM flags")
    process_messages(FakeClaude(verdict("interview_request", "Bright Labs")), conn, config, [old], now=NOW)
    assert conn.execute("SELECT COUNT(*) FROM flags").fetchone()[0] == 0  # a past call raises nothing
    assert invite.thread_id


def test_a_canceled_interview_closes_its_flag_even_if_listed_first(conn, config):
    """The Northwind case: a screen booked for Oct 13, then canceled and moved the same day."""
    from datetime import timedelta

    applied(conn, "Bright Labs", website="https://brightlabs.example")
    booking = parse_eml(EMAILS / "interview_invite.eml", gmail_id="book")
    cancel = parse_eml(EMAILS / "interview_invite.eml", gmail_id="cancel")
    cancel.received_at = booking.received_at + timedelta(hours=1)
    client = FakeClaude(verdict("interview_request", "Bright Labs", event="2026-10-13", summary="screen on Oct 13"),
                        verdict("other", "Bright Labs", event="2026-10-13", cancels=True, summary="Oct 13 screen canceled"))
    process_messages(client, conn, config, [cancel, booking], now=NOW)  # Gmail lists newest first
    flag = conn.execute("SELECT * FROM flags WHERE kind = 'interview_request'").fetchone()
    assert flag["status"] == "resolved" and "canceled or moved" in flag["message"]
    assert "(email of Oct 12)" in flag["message"]  # every flag says which email it came from


def test_flags_for_past_dates_close_by_themselves(conn, config):
    from datetime import timedelta

    from applier.mail.process import expire_past_flags

    applied(conn, "Bright Labs", website="https://brightlabs.example")
    process_messages(FakeClaude(verdict("interview_request", "Bright Labs", event="2026-10-13")), conn, config,
                     [parse_eml(EMAILS / "interview_invite.eml")], now=NOW)
    assert conn.execute("SELECT status FROM flags").fetchone()[0] == "open"
    assert expire_past_flags(conn, NOW + timedelta(days=2)) == 1
    assert conn.execute("SELECT status FROM flags").fetchone()[0] == "resolved"
