"""Submitting an application the agent fills (brief 6, step 4): Greenhouse and Lever, and Ashby only when you
set config.ashby_submit to auto.

Every rule is checked twice: before the form opens and again right before the Submit click (PAUSE,
enabled, mode, the 08:00 to 20:00 window, the daily cap, the do-not-apply folder, your approval). The form
must fill and read back without a single problem or open question. A bot check at any point stops it
(rule 3); a posting that asks applicants not to use AI is never submitted by the agent (rule 6). If no
confirmation page appears, the application is recorded as submitted_unconfirmed and is never sent twice.
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from datetime import datetime
from typing import Any

from applier import db
from applier.apply import fields as fields_mod
from applier.apply import fill as fill_mod
from applier.apply.session import (CONFIRMATION, SessionResult, StopApplication, _launch, _packet, _playwright, _stamp,
                                   prepare_on_page)
from applier.config import Config
from applier.paths import Paths
from applier.safety import submission_gate
from applier.tailor.packet import _load_posting

SUBMIT_BUTTON = {
    "greenhouse": "#application-form button[type=submit], #application-form button:has-text('Submit application')",
    "lever": "#btn-submit, button:has-text('Submit application')",
    "ashby": "button:has-text('Submit Application')",
}
CONFIRMED_URL = re.compile(r"/(confirmation|thanks|thank-you|thank_you|success)\b", re.I)
WAIT_SECONDS = 45


def _check_gate(conn, config, paths, application_id: int, now: datetime | None, when: str) -> None:
    app = conn.execute("SELECT * FROM applications WHERE id = ?", (application_id,)).fetchone()
    gate = submission_gate(conn, config, paths, now, application=app)
    if not gate.allowed:
        raise StopApplication(f"not submitted ({when}): " + "; ".join(gate.reasons))


def _confirmed(page) -> str | None:
    """The confirmation page's text, or None while it has not appeared."""
    try:
        text = fields_mod.page_text(page)
        form_left = page.locator("[data-field-path], #application-form .application-question, #application-form input").count() == 0
    except Exception:
        return None
    if CONFIRMATION.search(text) and (form_left or CONFIRMED_URL.search(page.url)):
        return text
    if CONFIRMED_URL.search(page.url) and CONFIRMATION.search(text):
        return text
    return None


def submit_application(client: Any, conn: sqlite3.Connection, config: Config, paths: Paths, application_id: int, *,
                       now: datetime | None = None, headless: bool = False) -> SessionResult:
    app = conn.execute("SELECT * FROM applications WHERE id = ?", (application_id,)).fetchone()
    if app is None:
        raise StopApplication(f"no application #{application_id}")
    row = _load_posting(conn, app["posting_id"])
    if row["ats"] not in SUBMIT_BUTTON:
        raise StopApplication(f"{row['ats']} forms are not supported")
    if row["ats"] == "ashby" and config.ashby_submit != "auto":
        raise StopApplication("Ashby forms use assisted submission: use 'assist' and click Submit yourself")
    if row["ai_policy_note"]:
        db.add_flag(conn, "ai_tools_prohibited", f"{row['company_name']}: asks applicants not to use AI; submit it "
                    "yourself if you want to apply", application_id=application_id)
        raise StopApplication("the employer asks applicants not to use AI tools; the agent will not submit it (rule 6)")
    _check_gate(conn, config, paths, application_id, now, "before opening the form")
    result = SessionResult(row["id"], row["company_name"], row["title"], application_id=application_id)
    directory, packet = _packet(client, conn, config, paths, row)
    with _playwright(paths) as pw:
        browser, page = _launch(pw, paths, headless)
        try:
            page.goto(row["apply_url"] or row["url"], wait_until="domcontentloaded", timeout=60000)
            try:
                prepare_on_page(client, conn, config, paths, row, page, directory, packet, result)
            except StopApplication as exc:  # a bot check or a form that did not load: it needs you
                db.set_status(conn, application_id, "needs_me", f"not submitted: {str(exc)[:200]}")
                raise
            result.screenshots.append(str(fill_mod.screenshot(page, directory / "screenshots" / f"before_submit_{_stamp()}.png")))
            if result.problems or result.needs_me:
                db.set_status(conn, application_id, "needs_me", "not submitted: the form did not fill cleanly")
                raise StopApplication("the form did not fill cleanly; nothing was submitted")
            _check_gate(conn, config, paths, application_id, now, "right before Submit")
            page.locator(SUBMIT_BUTTON[row["ats"]]).first.click()
            confirmation, deadline = None, time.monotonic() + WAIT_SECONDS
            while time.monotonic() < deadline and confirmation is None:
                time.sleep(1.5)
                problems = fields_mod.page_problems(page)
                if problems:  # a check appeared after the click: never solve it (rule 3)
                    db.add_flag(conn, "bot_check", f"{row['company_name']} | {row['title']}: {problems[0]} after Submit; "
                                "check by hand whether it went through", severity="urgent", application_id=application_id)
                    break
                confirmation = _confirmed(page)
            result.screenshots.append(str(fill_mod.screenshot(page, directory / "screenshots" / f"after_submit_{_stamp()}.png")))
            if confirmation:
                (directory / "confirmation.txt").write_text(confirmation[:5000], encoding="utf-8")
                db.mark_submitted(conn, application_id, "submitted", db.utc_now())
                result.submitted, result.status = True, "submitted"
            else:  # it may or may not have gone through: never send it again
                db.mark_submitted(conn, application_id, "submitted_unconfirmed", db.utc_now())
                db.add_flag(conn, "unconfirmed_submission", f"{row['company_name']} | {row['title']}: Submit was clicked "
                            "but no confirmation page appeared; it will not be sent again. Watch your inbox.",
                            application_id=application_id)
                result.status = "submitted_unconfirmed"
            with conn:
                conn.execute("UPDATE applications SET confirmation_text = ?, mode = ?, screenshots_json = ? WHERE id = ?",
                             ((confirmation or "")[:5000] or None, config.mode if config.mode != "dry_run" else None,
                              json.dumps(result.screenshots), application_id))
        except StopApplication as exc:  # a gate that closed in the meantime keeps the application prepared
            result.problems.append(str(exc))
        finally:
            browser.close()
    return result
