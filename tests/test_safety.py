"""Rule 8: pace limits, time window, PAUSE file, master switch, approvals, budget."""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from applier import db
from applier.blocklist import sync_company_blocks
from applier.safety import budget_status, submission_gate
from tests.conftest import make_company

NY = ZoneInfo("America/New_York")
NOON_SUMMER = datetime(2026, 7, 14, 12, 0, tzinfo=NY)
NOON_WINTER = datetime(2026, 1, 14, 12, 0, tzinfo=NY)


@pytest.fixture
def live_config(config):
    """Settings as they would be in auto mode after the first approvals."""
    return config.model_copy(update={"mode": "auto", "enabled": True, "review_first_n": 0})


def test_allowed_when_every_condition_is_met(conn, live_config, paths):
    gate = submission_gate(conn, live_config, paths, NOON_SUMMER)
    assert gate.allowed, gate.reasons


def test_pause_file_stops_all_submissions(conn, live_config, paths):
    """Acceptance test: creating PAUSE stops all submissions."""
    paths.pause_file.write_text("", encoding="utf-8")
    gate = submission_gate(conn, live_config, paths, NOON_SUMMER)
    assert not gate.allowed
    assert any("PAUSE" in reason for reason in gate.reasons)


def test_master_switch_off_stops_submissions(conn, live_config, paths):
    gate = submission_gate(conn, live_config.model_copy(update={"enabled": False}), paths, NOON_SUMMER)
    assert not gate.allowed


def test_dry_run_never_submits(conn, live_config, paths):
    gate = submission_gate(conn, live_config.model_copy(update={"mode": "dry_run"}), paths, NOON_SUMMER)
    assert not gate.allowed
    assert any("dry_run" in reason for reason in gate.reasons)


@pytest.mark.parametrize("day", [NOON_SUMMER, NOON_WINTER], ids=["summer time", "winter time"])
@pytest.mark.parametrize(
    "hour, minute, allowed",
    [(7, 59, False), (8, 0, True), (19, 59, True), (20, 0, False), (2, 0, False)],
)
def test_new_york_submit_window(conn, live_config, paths, day, hour, minute, allowed):
    moment = day.replace(hour=hour, minute=minute).astimezone(timezone.utc)
    assert submission_gate(conn, live_config, paths, moment).allowed is allowed


def test_daily_limit(conn, live_config, paths):
    for i in range(live_config.max_applications_per_day):
        company = make_company(conn, f"Company {i}")
        app = db.create_application(conn, company, "Associate")
        db.mark_submitted(conn, app, "submitted", NOON_SUMMER - timedelta(hours=1))
    gate = submission_gate(conn, live_config, paths, NOON_SUMMER)
    assert not gate.allowed
    assert any("daily limit" in reason for reason in gate.reasons)
    # The next New York day starts fresh.
    assert submission_gate(conn, live_config, paths, NOON_SUMMER + timedelta(days=1)).allowed


def test_review_mode_waits_for_approval(conn, live_config, paths):
    company = make_company(conn)
    app_id = db.create_application(conn, company, "Associate", status="prepared")
    review = live_config.model_copy(update={"mode": "review"})

    app = conn.execute("SELECT * FROM applications WHERE id = ?", (app_id,)).fetchone()
    gate = submission_gate(conn, review, paths, NOON_SUMMER, application=app)
    assert not gate.allowed and "waiting for your approval" in gate.reasons

    conn.execute("UPDATE applications SET approved_at = '2026-07-14T15:00:00Z' WHERE id = ?", (app_id,))
    app = conn.execute("SELECT * FROM applications WHERE id = ?", (app_id,)).fetchone()
    assert submission_gate(conn, review, paths, NOON_SUMMER, application=app).allowed


def test_first_applications_need_approval_even_in_auto(conn, live_config, paths):
    company = make_company(conn)
    app_id = db.create_application(conn, company, "Associate", status="prepared")
    app = conn.execute("SELECT * FROM applications WHERE id = ?", (app_id,)).fetchone()
    auto_first_ten = live_config.model_copy(update={"review_first_n": 10})
    gate = submission_gate(conn, auto_first_ten, paths, NOON_SUMMER, application=app)
    assert "waiting for your approval" in gate.reasons


def test_only_prepared_applications_can_be_submitted(conn, live_config, paths):
    company = make_company(conn)
    app_id = db.create_application(conn, company, "Associate", status="needs_me")
    app = conn.execute("SELECT * FROM applications WHERE id = ?", (app_id,)).fetchone()
    assert not submission_gate(conn, live_config, paths, NOON_SUMMER, application=app).allowed


def test_unreachable_blocklist_folder_stops_submissions(conn, live_config, paths, jobs_dir):
    jobs_dir.rename(jobs_dir.with_name("Jobs-moved"))
    gate = submission_gate(conn, live_config, paths, NOON_SUMMER)
    assert not gate.allowed
    assert any("do-not-apply folder" in reason for reason in gate.reasons)


def test_blocked_company_fails_the_gate(conn, live_config, paths):
    company = make_company(conn, "Some Startup")
    app_id = db.create_application(conn, company, "Associate", status="prepared")
    conn.execute("UPDATE applications SET approved_at = '2026-07-14T15:00:00Z' WHERE id = ?", (app_id,))
    # The company is added to a list after the application was prepared.
    with paths.do_not_apply_csv.open("a", encoding="utf-8") as handle:
        handle.write("Some Startup,,yes,\n")
    sync_company_blocks(conn, paths, live_config)
    app = conn.execute("SELECT * FROM applications WHERE id = ?", (app_id,)).fetchone()
    gate = submission_gate(conn, live_config, paths, NOON_SUMMER, application=app)
    assert not gate.allowed


def test_daily_budget(conn, live_config):
    spent_at = db.to_db_time(NOON_SUMMER - timedelta(hours=2))
    conn.execute(
        "INSERT INTO api_usage (model, purpose, cost_usd, created_at) VALUES ('m', 'test', ?, ?)",
        (live_config.daily_budget_usd, spent_at),
    )
    assert not budget_status(conn, live_config, NOON_SUMMER).ok
    assert budget_status(conn, live_config, NOON_SUMMER + timedelta(days=1)).ok


def test_one_day_budget_override(conn, config):
    from datetime import date, datetime, timezone

    from applier.safety import budget_status

    config.budget_overrides = {date(2026, 10, 12): 6.0}
    assert budget_status(conn, config, datetime(2026, 10, 12, 23, 0, tzinfo=timezone.utc)).limit == 6.0
    assert budget_status(conn, config, datetime(2026, 10, 13, 15, 0, tzinfo=timezone.utc)).limit == config.daily_budget_usd
