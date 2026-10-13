"""Rule 5 and status history, enforced by the database itself."""

import sqlite3
from datetime import datetime, timezone

import pytest

from applier import db
from applier.blocklist import sync_company_blocks
from applier.companies import company_key
from tests.conftest import make_company


def add_posting(conn, company_id, posting_id, title):
    with conn:
        cur = conn.execute(
            "INSERT INTO postings (company_id, ats, ats_posting_id, title) VALUES (?, 'ashby', ?, ?)",
            (company_id, posting_id, title),
        )
    return cur.lastrowid


def test_second_posting_at_applied_company_is_refused(conn):
    """Acceptance test: a second posting at an already-applied company is skipped by the constraint."""
    company = make_company(conn, "Bright Labs")
    first = add_posting(conn, company, "p1", "Strategy & Operations Associate")
    second = add_posting(conn, company, "p2", "Chief of Staff")

    db.create_application(conn, company, "Strategy & Operations Associate", posting_id=first)
    with pytest.raises(db.AlreadyApplied):
        db.create_application(conn, company, "Chief of Staff", posting_id=second)
    assert conn.execute("SELECT COUNT(*) FROM applications").fetchone()[0] == 1


def test_constraint_holds_even_if_code_is_bypassed(conn):
    company = make_company(conn, "Bright Labs")
    key = company_key("Bright Labs")
    conn.execute("INSERT INTO applications (company_id, company_key, role_title) VALUES (?, ?, 'A')", (company, key))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO applications (company_id, company_key, role_title) VALUES (?, ?, 'B')", (company, key))


def test_application_key_must_match_its_company(conn):
    company = make_company(conn, "Bright Labs")
    with pytest.raises(sqlite3.IntegrityError, match="MISMATCH"):
        conn.execute(
            "INSERT INTO applications (company_id, company_key, role_title) VALUES (?, 'somethingelse', 'A')",
            (company,),
        )


def test_spelling_variants_are_one_company(conn):
    first = make_company(conn, "Spark AI, Inc.")
    second = make_company(conn, "SparkAI")
    assert first == second
    db.create_application(conn, first, "Operations Associate")
    with pytest.raises(db.AlreadyApplied):
        db.create_application(conn, second, "Program Lead")


def test_blocked_companies_never_get_an_application(conn, paths, config):
    sync_company_blocks(conn, paths, config)
    for name in ["Acme Robotics", "Northwind", "Owlbright", "Bluepeak AI"]:
        company = make_company(conn, name)
        with pytest.raises(db.CompanyBlocked):
            db.create_application(conn, company, "Associate")
    assert conn.execute("SELECT COUNT(*) FROM applications").fetchone()[0] == 0


def test_block_holds_even_if_code_is_bypassed(conn, paths, config):
    sync_company_blocks(conn, paths, config)
    company = make_company(conn, "Northwind")
    with pytest.raises(sqlite3.IntegrityError, match="BLOCKED"):
        conn.execute(
            "INSERT INTO applications (company_id, company_key, role_title) VALUES (?, 'northwind', 'A')", (company,)
        )


def test_every_status_change_is_recorded_with_a_timestamp(conn):
    company = make_company(conn)
    app = db.create_application(conn, company, "Associate", status="queued")
    db.set_status(conn, app, "prepared")
    db.set_status(conn, app, "needs_me", note="salary question not covered")
    db.set_status(conn, app, "needs_me")  # unchanged: no new event

    rows = conn.execute(
        "SELECT event_type, old_status, new_status, details, created_at FROM events "
        "WHERE application_id = ? ORDER BY id",
        (app,),
    ).fetchall()
    assert [(r["event_type"], r["old_status"], r["new_status"]) for r in rows] == [
        ("created", None, "queued"),
        ("status_change", "queued", "prepared"),
        ("status_change", "prepared", "needs_me"),
        ("note", None, None),
    ]
    assert rows[3]["details"] == "salary question not covered"
    assert all(r["created_at"].endswith("Z") for r in rows)


def test_unknown_statuses_are_rejected_by_the_database(conn):
    company = make_company(conn)
    app = db.create_application(conn, company, "Associate")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE applications SET status = 'whatever' WHERE id = ?", (app,))
    with pytest.raises(ValueError):
        db.set_status(conn, app, "whatever")


def test_company_of_an_application_cannot_change(conn):
    first = make_company(conn, "Bright Labs")
    other = make_company(conn, "Dim Labs")
    app = db.create_application(conn, first, "Associate")
    with pytest.raises(sqlite3.IntegrityError, match="FIXED"):
        conn.execute("UPDATE applications SET company_id = ? WHERE id = ?", (other, app))
    with pytest.raises(sqlite3.IntegrityError, match="FIXED"):
        conn.execute("UPDATE companies SET company_key = 'x' WHERE id = ?", (first,))


def test_reset_frees_the_company_and_keeps_history(conn):
    company = make_company(conn)
    app = db.create_application(conn, company, "Associate")
    db.set_status(conn, app, "closed")

    assert db.reset_company(conn, company_key("Bright Labs"), "posting closed before submission")
    new_app = db.create_application(conn, company, "Chief of Staff")
    assert new_app != app

    archived = conn.execute("SELECT * FROM applications_archive").fetchall()
    assert len(archived) == 1 and archived[0]["application_id"] == app
    old_events = conn.execute("SELECT event_type FROM events WHERE application_id = ?", (app,)).fetchall()
    assert [r["event_type"] for r in old_events] == ["created", "status_change", "company_reset"]


def test_submissions_are_counted_by_new_york_day(conn, config):
    tz = config.submit_window.tz
    company = make_company(conn)
    app = db.create_application(conn, company, "Associate")
    # 23:30 in New York on July 14 is already July 15 in UTC.
    db.mark_submitted(conn, app, "submitted", datetime(2026, 7, 15, 3, 30, tzinfo=timezone.utc))
    assert db.submissions_on(conn, datetime(2026, 7, 14).date(), tz) == 1
    assert db.submissions_on(conn, datetime(2026, 7, 15).date(), tz) == 0
