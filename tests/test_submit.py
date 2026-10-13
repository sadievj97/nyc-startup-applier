"""Phase 7: the agent submits Greenhouse and Lever forms only after every check, and never twice."""

from datetime import datetime, timezone

import pytest

from applier import db
from applier.apply.session import StopApplication, approve, dry_run
from applier.apply.submit import submit_application
from tests.conftest import make_company
from tests.test_apply import (GH_QUESTIONS, IN_WINDOW, make_posting, model_reply, needs_browser,  # noqa: F401
                              server)
from tests.test_tailor import FakeClaude, master  # noqa: F401 (fixtures)

LEVER_REPLY = model_reply({"key": "org", "category": "current_company", "answer": "", "confidence": "low"},
                          {"key": "urls[Other]", "category": "other", "answer": "", "worth_answering": False})


def prepared(conn, config, paths, make_posting, ats, page, name, replies=()):  # noqa: F811
    posting = make_posting(ats, page, name, {"questions": GH_QUESTIONS} if ats == "greenhouse" else None)
    result = dry_run(FakeClaude(*replies), conn, config, paths, posting, queue=True, headless=True)
    assert result.status == "prepared", result.problems + result.needs_me
    return posting, result.application_id


@needs_browser
def test_greenhouse_submits_after_approval_and_confirms(conn, config, paths, make_posting, server):  # noqa: F811
    _, app_id = prepared(conn, config, paths, make_posting, "greenhouse", "greenhouse_form.html?clean", "Coastal Labs")
    config.mode = "review"
    with pytest.raises(StopApplication, match="approval"):
        submit_application(FakeClaude(), conn, config, paths, app_id, now=IN_WINDOW, headless=True)
    approve(conn, config, app_id)
    result = submit_application(FakeClaude(), conn, config, paths, app_id, now=IN_WINDOW, headless=True)
    assert result.submitted and len(server.posts) == 1
    row = conn.execute("SELECT status, submitted_at, confirmation_text FROM applications WHERE id = ?", (app_id,)).fetchone()
    assert row["status"] == "submitted" and row["submitted_at"] and "Thank you" in row["confirmation_text"]
    with pytest.raises(StopApplication):  # never twice
        submit_application(FakeClaude(), conn, config, paths, app_id, now=IN_WINDOW, headless=True)
    assert len(server.posts) == 1


@needs_browser
def test_lever_submits(conn, config, paths, make_posting, server):  # noqa: F811
    _, app_id = prepared(conn, config, paths, make_posting, "lever", "lever_form.html?clean", "Acme Mobile", [LEVER_REPLY])
    config.mode = "review"
    approve(conn, config, app_id)
    result = submit_application(FakeClaude(LEVER_REPLY), conn, config, paths, app_id, now=IN_WINDOW, headless=True)
    assert result.submitted and len(server.posts) == 1


@needs_browser
def test_no_confirmation_means_unconfirmed_and_flagged(conn, config, paths, make_posting, server, monkeypatch):  # noqa: F811
    monkeypatch.setattr("applier.apply.submit.WAIT_SECONDS", 4)
    _, app_id = prepared(conn, config, paths, make_posting, "greenhouse", "greenhouse_form.html?clean&silent", "Quiet Labs")
    config.mode = "review"
    approve(conn, config, app_id)
    result = submit_application(FakeClaude(), conn, config, paths, app_id, now=IN_WINDOW, headless=True)
    assert not result.submitted and result.status == "submitted_unconfirmed"
    assert conn.execute("SELECT COUNT(*) FROM flags WHERE kind = 'unconfirmed_submission'").fetchone()[0] == 1
    with pytest.raises(StopApplication):
        submit_application(FakeClaude(), conn, config, paths, app_id, now=IN_WINDOW, headless=True)


@needs_browser
def test_outside_the_window_or_paused_nothing_is_sent(conn, config, paths, make_posting, server):  # noqa: F811
    _, app_id = prepared(conn, config, paths, make_posting, "greenhouse", "greenhouse_form.html?clean", "Night Labs")
    config.mode = "review"
    approve(conn, config, app_id)
    with pytest.raises(StopApplication, match="window"):
        submit_application(FakeClaude(), conn, config, paths, app_id,
                           now=datetime(2026, 10, 13, 3, 0, tzinfo=timezone.utc), headless=True)
    paths.pause_file.write_text("", encoding="utf-8")
    with pytest.raises(StopApplication, match="PAUSE"):
        submit_application(FakeClaude(), conn, config, paths, app_id, now=IN_WINDOW, headless=True)
    assert server.posts == []
    assert conn.execute("SELECT status FROM applications WHERE id = ?", (app_id,)).fetchone()[0] == "prepared"


@needs_browser
def test_no_ai_postings_are_never_agent_submitted(conn, config, paths, make_posting, server):  # noqa: F811
    posting, app_id = prepared(conn, config, paths, make_posting, "greenhouse", "greenhouse_form.html?clean", "Human Labs")
    with conn:
        conn.execute("UPDATE postings SET ai_policy_note = 'Please do not use AI tools to apply.' WHERE id = ?", (posting,))
    config.mode = "auto"
    with pytest.raises(StopApplication, match="rule 6"):
        submit_application(FakeClaude(), conn, config, paths, app_id, now=IN_WINDOW, headless=True)
    assert server.posts == []


def test_ashby_stays_assisted(conn, config, paths):
    company = make_company(conn, "Ash Labs")
    with conn:
        posting = conn.execute("INSERT INTO postings (company_id, ats, ats_posting_id, title) VALUES (?, 'ashby', 'a', 'Ops')",
                               (company,)).lastrowid
    app_id = db.create_application(conn, company, "Ops", posting_id=posting, ats="ashby", status="prepared")
    with pytest.raises(StopApplication, match="assisted"):
        submit_application(FakeClaude(), conn, config, paths, app_id, now=IN_WINDOW, headless=True)
