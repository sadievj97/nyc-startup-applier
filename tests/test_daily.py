"""Phase 7: the daily run, its catch-up rule, and one failing step not stopping the others."""

from datetime import datetime, time, timezone

from applier import db
from applier.apply.session import approve
from applier.daily import due, run_daily
from tests.test_apply import GH_QUESTIONS, IN_WINDOW, make_posting, needs_browser, server  # noqa: F401
from tests.test_tailor import FakeClaude, master  # noqa: F401 (fixtures)


def finished_run(conn, kind, when):
    run_id = db.start_run(conn, kind)
    db.finish_run(conn, run_id, "ok", "")
    with conn:
        conn.execute("UPDATE runs SET finished_at = ? WHERE id = ?", (db.to_db_time(when), run_id))


def ny(day, hour, minute=0):
    from zoneinfo import ZoneInfo

    return datetime(2026, 10, day, hour, minute, tzinfo=ZoneInfo("America/New_York")).astimezone(timezone.utc)


def test_catch_up_rule(conn, config):
    at = time(8, 15)
    assert due(conn, "daily", at, config, ny(13, 9))                # never ran
    finished_run(conn, "daily", ny(12, 8, 20))
    assert due(conn, "daily", at, config, ny(13, 9))                # yesterday's run, today's 08:15 has passed
    assert not due(conn, "daily", at, config, ny(13, 7))            # before 08:15: yesterday's run still counts
    finished_run(conn, "daily", ny(13, 12))                         # caught up at noon (PC was asleep)
    assert not due(conn, "daily", at, config, ny(13, 18))           # unlocking later the same day does nothing
    assert due(conn, "daily", at, config, ny(15, 22))               # the PC slept for two days: run once now


@needs_browser
def test_daily_prepares_then_submits_after_approval(conn, config, paths, make_posting, server):  # noqa: F811
    posting = make_posting("greenhouse", "greenhouse_form.html?clean", "Coastal Labs", {"questions": GH_QUESTIONS})
    with conn:
        conn.execute("UPDATE postings SET decision = 'chosen', score = 85 WHERE id = ?", (posting,))
    config.mode = "review"
    first = run_daily(FakeClaude(), conn, config, paths, now=IN_WINDOW, headless=True, log=lambda line: None,
                      steps=("prepare", "submit"))
    assert first.prepared and "prepared" in first.prepared[0]
    assert first.submitted == [] and "approval" in first.waiting[0]  # review mode: waits for you
    app_id = conn.execute("SELECT id FROM applications").fetchone()[0]
    approve(conn, config, app_id)
    second = run_daily(FakeClaude(), conn, config, paths, now=IN_WINDOW, headless=True, log=lambda line: None,
                       steps=("submit",))
    assert second.submitted == ["Coastal Labs | Data Analyst: submitted"] and len(server.posts) == 1
    assert (paths.data_dir / "applications.csv").exists()  # the finish step always runs


def test_dry_run_mode_never_creates_applications_or_submits(conn, config, paths, monkeypatch):
    calls = []
    config.mode = "dry_run"
    monkeypatch.setattr("applier.apply.session.dry_run", lambda *a, **k: calls.append(k) or None)
    result = run_daily(FakeClaude(), conn, config, paths, now=IN_WINDOW, log=lambda line: None, steps=("prepare", "submit"))
    assert any("skipped (mode is dry_run)" in s for s in result.steps)
    assert conn.execute("SELECT COUNT(*) FROM applications").fetchone()[0] == 0


def test_a_failing_step_does_not_stop_the_others(conn, config, paths, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("board down")

    monkeypatch.setattr("applier.jobs.pipeline.fetch_all", broken)
    result = run_daily(None, conn, config, paths, now=IN_WINDOW, log=lambda line: None, steps=("boards", "prepare"))
    assert any("boards: failed" in s for s in result.steps)
    assert any("prepare: skipped" in s for s in result.steps)  # no API key: model steps are skipped, not crashed
    assert any(s.startswith("finish:") for s in result.steps)
    assert conn.execute("SELECT COUNT(*) FROM flags WHERE kind = 'api_error'").fetchone()[0] == 1
    assert conn.execute("SELECT status FROM runs WHERE kind = 'daily'").fetchone()[0] == "ok"  # not repeated all day
