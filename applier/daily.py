"""The daily run (brief 9): every step in order, each one able to fail without stopping the others.

1. Companies: companies.csv every day; Hacker News "Who is hiring?" and funding proposals once a week.
2. Job boards: detect, fetch, filter, score, one posting per company.
3. Startup facts for new companies (web search), a few per day.
4. Email: sort new job emails, update statuses, raise flags.
5. Highlights and networking (people and notes for a couple of companies a day).
6. Prepare: fill the forms of the chosen postings (never submits); in review and auto modes this creates the
   company's one application, which waits for your approval in review mode.
7. Submit: approved Greenhouse and Lever applications (auto mode: also the ones that no longer need your
   approval), only inside the 08:00 to 20:00 window and under the daily cap. Ashby stays assisted.
8. Unconfirmed submissions older than 48 hours are flagged; folders, applications.csv and notifications.

Catch-up (the PC sleeps, so no wake timers): `due()` tells whether the most recent scheduled time has passed
since the last successful run; the scheduled task runs at log on and on unlock too, and a run that is not due
exits at once. A late run never moves submissions outside the window: prepared applications wait.
"""

from __future__ import annotations

import json
import sqlite3
import traceback
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone
from typing import Any, Callable

from applier import db
from applier.companies import load_aliases
from applier.config import Config, load_targets
from applier.llm import BudgetReached
from applier.paths import Paths
from applier.safety import budget_status, submission_gate

WEEK = timedelta(days=7)


@dataclass
class DailyResult:
    steps: list[str] = field(default_factory=list)
    prepared: list[str] = field(default_factory=list)
    submitted: list[str] = field(default_factory=list)
    waiting: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    cost_usd: float = 0.0
    skipped: bool = False


def due(conn: sqlite3.Connection, kind: str, at: time, config: Config, now: datetime | None = None) -> bool:
    """True if the latest scheduled time (today's if it has passed, else yesterday's) came after the last
    successful run of this kind: the run was missed or has not happened yet."""
    now = now or datetime.now(timezone.utc)
    tz = config.submit_window.tz
    local = now.astimezone(tz)
    slot = datetime.combine(local.date(), at, tz)
    if local < slot:
        slot -= timedelta(days=1)
    last = conn.execute("SELECT MAX(finished_at) FROM runs WHERE kind = ? AND status = 'ok'", (kind,)).fetchone()[0]
    return last is None or db.from_db_time(last) < slot.astimezone(timezone.utc)


def _meta(conn: sqlite3.Connection, key: str) -> datetime | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return db.from_db_time(row[0]) if row else None


def _set_meta(conn: sqlite3.Connection, key: str, when: datetime) -> None:
    with conn:
        conn.execute("INSERT INTO meta(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                      (key, db.to_db_time(when)))


def _waiting(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("""SELECT a.*, c.name AS company FROM applications a JOIN companies c ON c.id = a.company_id
                           WHERE a.status IN ('queued', 'prepared', 'needs_me') ORDER BY a.id""").fetchall()


def run_daily(client: Any, conn: sqlite3.Connection, config: Config, paths: Paths, *, gmail: Any = None,
              now: datetime | None = None, log: Callable[[str], None] = print, headless: bool = False,
              steps: tuple[str, ...] = ("companies", "boards", "research", "email", "network", "prepare", "submit")
              ) -> DailyResult:
    from applier.jobs.http import PoliteClient

    now = now or datetime.now(timezone.utc)
    result = DailyResult()
    run_id = db.start_run(conn, "daily", config.mode)
    spent_before = budget_status(conn, config, now).spent
    models_ok = client is not None

    def step(name: str, action: Callable[[], str | None], uses_model: bool = False) -> None:
        nonlocal models_ok
        if name not in steps and name not in ("finish",):
            return
        if uses_model and not models_ok:
            result.steps.append(f"{name}: skipped (no API key or the daily budget is used up)")
            log(result.steps[-1])
            return
        try:
            note = action()
            result.steps.append(f"{name}: {note or 'done'}")
        except BudgetReached:
            models_ok = False
            db.add_flag(conn, "budget_reached", f"Daily Claude API budget reached during the {name} step")
            result.steps.append(f"{name}: stopped, daily budget reached")
        except Exception as exc:  # one failing step never stops the others
            result.problems.append(f"{name}: {exc}")
            result.steps.append(f"{name}: failed ({str(exc)[:120]})")
            db.add_flag(conn, "api_error", f"daily run, {name} step: {str(exc)[:300]}")
            paths.logs_dir.mkdir(parents=True, exist_ok=True)
            with (paths.logs_dir / "daily_errors.log").open("a", encoding="utf-8") as handle:
                handle.write(f"{now.isoformat()} {name}\n{traceback.format_exc()}\n")
        log(result.steps[-1])

    def companies() -> str:
        from applier.jobs.pipeline import discover_hn
        from applier.jobs.store import sync_companies_csv

        aliases = load_aliases(paths.aliases_csv)
        listed = sync_companies_csv(conn, paths, aliases)
        note = f"{listed} companies in companies.csv"
        last = _meta(conn, "weekly_discovery")
        if last is None or now - last >= WEEK:
            with PoliteClient() as http:
                hn = discover_hn(conn, http, aliases)
            note += f"; Hacker News: {len(hn.companies)} New York posts"
            if models_ok:
                from applier.jobs import research

                funded = research.propose_funded(client, conn, config, aliases, 30)
                note += f"; funding news: {len(funded.proposals)} startups proposed for your approval"
            _set_meta(conn, "weekly_discovery", now)
        return note

    def boards() -> str:
        from applier.jobs.pipeline import detect_missing, fetch_all
        from applier.jobs.store import choose_per_company, score_all

        with PoliteClient() as http:
            detect_missing(conn, http, 20)
            summary = fetch_all(conn, http, load_targets(paths.targets_file), now)
        score_all(conn, load_targets(paths.targets_file), config)
        chosen = choose_per_company(conn, config)
        return (f"{summary.boards} boards, {summary.counts.new} new postings, {summary.counts.closed} closed; "
                f"{chosen.get('chosen', 0)} chosen")

    def research_step() -> str:
        from applier.jobs import research

        rows = research.companies_to_research(conn, False, None, 5)
        with PoliteClient() as http:
            for row in rows:
                research.research_company(client, conn, config, row, run_id, http=http)
        if rows:  # new facts can change the startup filter
            from applier.jobs.store import choose_per_company, score_all

            score_all(conn, load_targets(paths.targets_file), config)
            choose_per_company(conn, config)
        return f"{len(rows)} companies checked"

    def email() -> str:
        if gmail is None:
            return "skipped (Gmail not connected)"
        from applier.mail.process import sync_inbox

        sync = sync_inbox(client, conn, config, gmail, now)
        return f"{sync.classified} job emails sorted, {len(sync.status_changes)} status changes, {len(sync.flags)} flags"

    def network() -> str:
        from applier.network import companies_to_network, draft_note, find_highlights, find_people

        new = find_highlights(conn, config)
        people = 0
        for company, posting in companies_to_network(conn)[:2]:
            outcome = find_people(client, conn, config, company, posting)
            for person_id in outcome.added:
                draft_note(client, conn, config, paths, conn.execute("SELECT * FROM people WHERE id = ?", (person_id,)).fetchone())
            people += len(outcome.added)
        return f"{len(new)} new highlights, {people} people found"

    def prepare() -> str:
        from applier.apply.session import StopApplication, dry_run

        waiting = len(_waiting(conn))
        slots = max(0, config.max_applications_per_day - waiting)
        queue = config.mode != "dry_run"
        chosen = conn.execute("""SELECT p.id, c.name, p.title, p.ats FROM postings p JOIN companies c ON c.id = p.company_id
                                 WHERE p.decision = 'chosen' AND p.closed_at IS NULL AND p.ats IN ('ashby', 'greenhouse', 'lever')
                                 AND NOT EXISTS (SELECT 1 FROM applications a WHERE a.company_id = p.company_id)
                                 ORDER BY p.score DESC, p.first_seen DESC LIMIT ?""", (slots,)).fetchall()
        for p in chosen:
            try:
                session = dry_run(client, conn, config, paths, p["id"], queue=queue, headless=headless)
                result.prepared.append(f"{p['name']} | {p['title']}: {session.status}"
                                       + (f" ({len(session.needs_me)} question(s) for you)" if session.needs_me else ""))
            except StopApplication as exc:
                result.prepared.append(f"{p['name']} | {p['title']}: stopped ({exc})")
        return f"{len(chosen)} prepared ({waiting} were already waiting for you)"

    def submit() -> str:
        from applier.apply.session import StopApplication
        from applier.apply.submit import submit_application

        if config.mode == "dry_run":
            return "skipped (mode is dry_run)"
        for app in conn.execute("SELECT a.*, c.name AS company FROM applications a JOIN companies c ON c.id = a.company_id "
                                "WHERE a.status = 'prepared' ORDER BY a.approved_at IS NULL, a.id").fetchall():
            agent_submits = app["ats"] in ("greenhouse", "lever") or (app["ats"] == "ashby" and config.ashby_submit == "auto")
            if not agent_submits:
                if app["approved_at"]:
                    result.waiting.append(f"{app['company']} | {app['role_title']}: Ashby, ready for you to submit (assist {app['id']})")
                continue
            gate = submission_gate(conn, config, paths, now, application=app)
            if not gate.allowed:
                result.waiting.append(f"{app['company']} | {app['role_title']}: waiting ({'; '.join(gate.reasons)})")
                continue
            try:
                session = submit_application(client, conn, config, paths, app["id"], now=now, headless=headless)
                result.submitted.append(f"{app['company']} | {app['role_title']}: {session.status}")
            except StopApplication as exc:
                result.waiting.append(f"{app['company']} | {app['role_title']}: {exc}")
        return f"{len(result.submitted)} submitted"

    def finish() -> str:
        from applier.apply.folders import sync_folders
        from applier.notify import notify_urgent

        cutoff = db.to_db_time(now - timedelta(hours=48))
        for app in conn.execute("""SELECT a.*, c.name AS company FROM applications a JOIN companies c ON c.id = a.company_id
                                   WHERE a.status = 'submitted_unconfirmed' AND a.submitted_at < ?""", (cutoff,)):
            db.add_flag(conn, "unconfirmed_submission", f"{app['company']} | {app['role_title']}: still unconfirmed after "
                        "48 hours; check the company's page or your inbox. It will not be sent again.",
                        application_id=app["id"])
        sync_folders(conn, config, paths)
        sent = notify_urgent(conn, paths)
        return f"folders and applications.csv updated; {sent} urgent notification(s)"

    step("companies", companies)
    step("boards", boards)
    step("research", research_step, uses_model=True)
    step("email", email, uses_model=True)
    step("network", network, uses_model=True)
    step("prepare", prepare, uses_model=True)
    step("submit", submit, uses_model=True)
    step("finish", finish)
    result.cost_usd = round(budget_status(conn, config, datetime.now(timezone.utc)).spent - spent_before, 4)
    # The run counts as done even if a step failed (that step raised a flag), so it is not repeated all day.
    db.finish_run(conn, run_id, "ok",
                  json.dumps({"steps": result.steps, "prepared": result.prepared, "submitted": result.submitted,
                              "cost_usd": result.cost_usd})[:4000])
    return result
