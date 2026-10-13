"""The submission gate (rule 8). Every code path that could submit an application must call
`submission_gate` immediately before submitting, and must not submit unless it says allowed.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from applier import db
from applier.config import Config
from applier.paths import Paths


@dataclass
class GateResult:
    allowed: bool
    reasons: list[str] = field(default_factory=list)


def in_submit_window(config: Config, now: datetime) -> bool:
    local = now.astimezone(config.submit_window.tz).time()
    return config.submit_window.start <= local < config.submit_window.end


def submission_gate(
    conn: sqlite3.Connection,
    config: Config,
    paths: Paths,
    now: datetime | None = None,
    application: sqlite3.Row | None = None,
) -> GateResult:
    """Return whether a submission may happen now, with every reason it may not."""
    now = now or db.utc_now()
    tz = config.submit_window.tz
    reasons: list[str] = []

    if paths.pause_file.exists():
        reasons.append("A PAUSE file exists in the project folder")
    if not config.enabled:
        reasons.append("enabled is false in config.yaml")
    if config.mode == "dry_run":
        reasons.append("mode is dry_run, which never submits")
    if not in_submit_window(config, now):
        window = config.submit_window
        reasons.append(f"outside the {window.start:%H:%M} to {window.end:%H:%M} New York submit window")

    used = db.submissions_on(conn, now.astimezone(tz).date(), tz)
    if used >= config.max_applications_per_day:
        reasons.append(f"daily limit reached ({used} of {config.max_applications_per_day} today)")

    for folder in config.do_not_apply_folders:
        if not Path(folder).is_dir():
            reasons.append(f"cannot reach the do-not-apply folder {folder}")

    if application is not None:
        if application["status"] != "prepared":
            reasons.append(f"application status is {application['status']}, not prepared")
        if db.is_blocked(conn, application["company_key"]):
            reasons.append("company is on a do-not-apply or applied-before list")
        needs_approval = config.mode == "review" or db.submissions_total(conn) < config.review_first_n
        if needs_approval and not application["approved_at"]:
            reasons.append("waiting for your approval")

    return GateResult(allowed=not reasons, reasons=reasons)


@dataclass
class BudgetStatus:
    spent: float
    limit: float

    @property
    def ok(self) -> bool:
        return self.spent < self.limit

    @property
    def remaining(self) -> float:
        return max(0.0, self.limit - self.spent)


def budget_status(conn: sqlite3.Connection, config: Config, now: datetime | None = None) -> BudgetStatus:
    """Claude API spending today (New York day). Model calls must stop when not ok."""
    now = now or db.utc_now()
    tz = config.submit_window.tz
    day = now.astimezone(tz).date()
    limit = config.budget_overrides.get(day, config.daily_budget_usd)  # a one-day limit you set, if any
    return BudgetStatus(spent=db.spend_on(conn, day, tz), limit=limit)
