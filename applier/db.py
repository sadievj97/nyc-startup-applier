"""SQLite storage at data/applier.db.

Rules enforced by the database itself, so no code path can skip them:
- one application per company: UNIQUE(applications.company_key)   (rule 5)
- no application to a blocked company: trigger on company_blocks   (rule 5)
- every status change is recorded with a timestamp: trigger on applications.status
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

SCHEMA_VERSION = 2

APPLICATION_STATUSES = (
    "discovered",
    "queued",
    "prepared",
    "needs_me",
    "submitted_unconfirmed",
    "submitted",
    "confirmation_received",
    "assessment",
    "interview",
    "offer",
    "rejected",
    "closed",
    "withdrawn",
)
# Statuses that mean an application actually went out (counted against the daily cap).
SUBMITTED_STATUSES = APPLICATION_STATUSES[4:]

ATS_TYPES = ("ashby", "greenhouse", "lever", "workable", "manual", "unknown")

POSTING_DECISIONS = (
    "new",
    "eligible",
    "chosen",
    "skipped_one_per_company",
    "below_min_score",
    "filtered_location",
    "filtered_sponsorship",
    "filtered_role",
    "filtered_salary",
    "filtered_industry",
    "filtered_startup",
    "blocked_company",
    "manual",
    "closed",
)

BLOCK_REASONS = ("do_not_apply", "applied_before", "jobs_folder")

FLAG_KINDS = (
    # urgent
    "interview_request",
    "assessment",
    "offer",
    "work_authorization_question",
    "executive_email",
    "due_soon",
    # other
    "bot_check",
    "unanswerable_question",
    "fact_check_failed",
    "posting_closed",
    "possible_duplicate",
    "api_error",
    "budget_reached",
    "gmail_reauth",
    "ai_tools_prohibited",
    "low_confidence_answer",
    "unexpected_field",
    "submit_failed",
    "unconfirmed_submission",
    "company_facts_uncertain",
    "blocklist_unavailable",
    "salary_needs_me",
    "other",
)

EMAIL_CATEGORIES = (
    "confirmation",
    "rejection",
    "assessment",
    "interview_request",
    "recruiter_outreach",
    "offer",
    "information_request",
    "other",
)

_NOW = "(strftime('%Y-%m-%dT%H:%M:%SZ','now'))"


def _in(values: tuple[str, ...]) -> str:
    return "(" + ", ".join(f"'{v}'" for v in values) + ")"


SCHEMA = f"""
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS companies (
    id               INTEGER PRIMARY KEY,
    name             TEXT NOT NULL,
    company_key      TEXT NOT NULL UNIQUE,
    website          TEXT,
    careers_url      TEXT,
    ats              TEXT NOT NULL DEFAULT 'unknown' CHECK (ats IN {_in(ATS_TYPES)}),
    board_token      TEXT,
    industry         TEXT,
    discovery_source TEXT NOT NULL DEFAULT 'companies_csv',
    discovery_status TEXT NOT NULL DEFAULT 'approved'
                     CHECK (discovery_status IN ('approved', 'proposed', 'dismissed')),
    notes            TEXT,
    created_at       TEXT NOT NULL DEFAULT {_NOW},
    updated_at       TEXT NOT NULL DEFAULT {_NOW}
);

-- Startup facts (founded year, headcount, E-Verify, LCA filings...) with where each came from.
CREATE TABLE IF NOT EXISTS company_facts (
    id           INTEGER PRIMARY KEY,
    company_id   INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    fact         TEXT NOT NULL,
    value        TEXT,
    source_url   TEXT,
    uncertain    INTEGER NOT NULL DEFAULT 0 CHECK (uncertain IN (0, 1)),
    retrieved_at TEXT NOT NULL DEFAULT {_NOW}
);

-- Companies that must never get an application. Rebuilt from the CSVs and folders on each run.
CREATE TABLE IF NOT EXISTS company_blocks (
    company_key  TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    reason       TEXT NOT NULL CHECK (reason IN {_in(BLOCK_REASONS)}),
    highlight    INTEGER NOT NULL DEFAULT 1 CHECK (highlight IN (0, 1)),
    source       TEXT NOT NULL,
    synced_at    TEXT NOT NULL DEFAULT {_NOW}
);

CREATE TABLE IF NOT EXISTS postings (
    id                INTEGER PRIMARY KEY,
    company_id        INTEGER NOT NULL REFERENCES companies(id),
    ats               TEXT NOT NULL CHECK (ats IN {_in(ATS_TYPES)}),
    ats_posting_id    TEXT NOT NULL,
    title             TEXT NOT NULL,
    department        TEXT,
    team              TEXT,
    location          TEXT,
    other_locations   TEXT,  -- JSON list of further locations the posting names
    workplace_type    TEXT NOT NULL DEFAULT 'unknown'
                      CHECK (workplace_type IN ('onsite', 'hybrid', 'remote', 'unknown')),
    employment_type   TEXT,
    is_listed         INTEGER NOT NULL DEFAULT 1 CHECK (is_listed IN (0, 1)),
    url               TEXT,
    apply_url         TEXT,
    pay_min           REAL,
    pay_max           REAL,
    pay_currency      TEXT,
    pay_interval      TEXT,
    pay_raw           TEXT,
    published_at      TEXT,
    source_updated_at TEXT,
    first_seen        TEXT NOT NULL DEFAULT {_NOW},
    last_seen         TEXT NOT NULL DEFAULT {_NOW},
    closed_at         TEXT,
    description_raw   TEXT,
    description_text  TEXT,
    extra_json        TEXT,  -- ATS-specific facts, e.g. Greenhouse include_ai_disclaimer
    required_years    INTEGER,
    sponsorship_note  TEXT,  -- the sentence that says the company cannot (or can) sponsor
    ai_policy_note    TEXT,  -- the sentence that asks applicants not to use AI (rule 6)
    score             INTEGER CHECK (score BETWEEN 0 AND 100),
    score_explanation TEXT,
    decision          TEXT NOT NULL DEFAULT 'new' CHECK (decision IN {_in(POSTING_DECISIONS)}),
    decision_reason   TEXT,
    UNIQUE (company_id, ats, ats_posting_id)
);

-- AUTOINCREMENT: ids are never reused, so history of a reset application never mixes with a new one.
CREATE TABLE IF NOT EXISTS applications (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id        INTEGER NOT NULL REFERENCES companies(id),
    company_key       TEXT NOT NULL UNIQUE,  -- rule 5: one application per company, ever
    posting_id        INTEGER REFERENCES postings(id),
    role_title        TEXT NOT NULL,
    url               TEXT,
    ats               TEXT CHECK (ats IS NULL OR ats IN {_in(ATS_TYPES)}),
    score             INTEGER,
    mode              TEXT CHECK (mode IS NULL OR mode IN ('dry_run', 'review', 'auto', 'assisted')),
    status            TEXT NOT NULL DEFAULT 'discovered' CHECK (status IN {_in(APPLICATION_STATUSES)}),
    resume_path       TEXT,
    cover_letter_path TEXT,
    packet_dir        TEXT,
    answers_json      TEXT,
    screenshots_json  TEXT,
    confirmation_text TEXT,
    needs_me_reason   TEXT,
    approved_at       TEXT,
    submitted_at      TEXT,
    created_at        TEXT NOT NULL DEFAULT {_NOW},
    updated_at        TEXT NOT NULL DEFAULT {_NOW}
);

-- Copies of applications removed by an explicit reset, so history is never lost.
CREATE TABLE IF NOT EXISTS applications_archive (
    archive_id     INTEGER PRIMARY KEY,
    application_id INTEGER NOT NULL,
    company_key    TEXT NOT NULL,
    snapshot_json  TEXT NOT NULL,
    reset_reason   TEXT,
    archived_at    TEXT NOT NULL DEFAULT {_NOW}
);

-- application_id has no foreign key on purpose: history must survive a reset.
CREATE TABLE IF NOT EXISTS events (
    id             INTEGER PRIMARY KEY,
    application_id INTEGER,
    company_id     INTEGER,
    posting_id     INTEGER,
    event_type     TEXT NOT NULL,
    old_status     TEXT,
    new_status     TEXT,
    details        TEXT,
    created_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE IF NOT EXISTS emails (
    id             INTEGER PRIMARY KEY,
    gmail_id       TEXT NOT NULL UNIQUE,
    thread_id      TEXT,
    from_address   TEXT,
    from_name      TEXT,
    subject        TEXT,
    received_at    TEXT,
    category       TEXT CHECK (category IS NULL OR category IN {_in(EMAIL_CATEGORIES)}),
    application_id INTEGER REFERENCES applications(id) ON DELETE SET NULL,
    company_id     INTEGER REFERENCES companies(id),
    confidence     REAL,
    summary        TEXT,
    due_at         TEXT,
    processed_at   TEXT NOT NULL DEFAULT {_NOW}
);

CREATE TABLE IF NOT EXISTS flags (
    id             INTEGER PRIMARY KEY,
    severity       TEXT NOT NULL CHECK (severity IN ('urgent', 'normal')),
    kind           TEXT NOT NULL CHECK (kind IN {_in(FLAG_KINDS)}),
    message        TEXT NOT NULL,
    application_id INTEGER,
    company_id     INTEGER REFERENCES companies(id),
    posting_id     INTEGER REFERENCES postings(id),
    email_id       INTEGER REFERENCES emails(id),
    due_at         TEXT,
    status         TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'resolved', 'dismissed')),
    created_at     TEXT NOT NULL DEFAULT {_NOW},
    resolved_at    TEXT
);

CREATE TABLE IF NOT EXISTS highlights (
    id          INTEGER PRIMARY KEY,
    company_id  INTEGER NOT NULL REFERENCES companies(id),
    posting_id  INTEGER NOT NULL UNIQUE REFERENCES postings(id),
    score       INTEGER CHECK (score IS NULL OR score BETWEEN 0 AND 100),
    reason      TEXT NOT NULL,
    people_json TEXT NOT NULL DEFAULT '[]',  -- name, title, source URL, why relevant; never contact details
    status      TEXT NOT NULL DEFAULT 'new' CHECK (status IN ('new', 'seen', 'dismissed')),
    created_at  TEXT NOT NULL DEFAULT {_NOW},
    updated_at  TEXT NOT NULL DEFAULT {_NOW}
);

-- Postings you chose to skip on the dashboard: never chosen again.
CREATE TABLE IF NOT EXISTS posting_skips (
    posting_id INTEGER PRIMARY KEY REFERENCES postings(id),
    reason     TEXT,
    created_at TEXT NOT NULL DEFAULT {_NOW}
);

-- Networking: people at a company, from public professional sources only (brief 4b). Never contact details.
CREATE TABLE IF NOT EXISTS people (
    id           INTEGER PRIMARY KEY,
    company_id   INTEGER NOT NULL REFERENCES companies(id),
    posting_id   INTEGER REFERENCES postings(id),
    name         TEXT NOT NULL,
    title        TEXT,
    source_url   TEXT NOT NULL,
    why          TEXT,
    note         TEXT,  -- a 1 to 2 line outreach draft you send yourself
    note_status  TEXT NOT NULL DEFAULT 'none' CHECK (note_status IN ('none', 'checked', 'failed_check')),
    status       TEXT NOT NULL DEFAULT 'new' CHECK (status IN ('new', 'contacted', 'dismissed')),
    created_at   TEXT NOT NULL DEFAULT {_NOW},
    UNIQUE (company_id, name)
);

-- Urgent flags already sent to you as a Windows notification (or Telegram).
CREATE TABLE IF NOT EXISTS notifications (
    flag_id INTEGER PRIMARY KEY REFERENCES flags(id),
    sent_at TEXT NOT NULL DEFAULT {_NOW}
);

CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY,
    kind        TEXT NOT NULL,
    mode        TEXT,
    status      TEXT NOT NULL DEFAULT 'running' CHECK (status IN ('running', 'ok', 'failed', 'stopped')),
    notes       TEXT,
    started_at  TEXT NOT NULL DEFAULT {_NOW},
    finished_at TEXT
);

CREATE TABLE IF NOT EXISTS api_usage (
    id                 INTEGER PRIMARY KEY,
    run_id             INTEGER REFERENCES runs(id),
    model              TEXT NOT NULL,
    purpose            TEXT NOT NULL,
    input_tokens       INTEGER NOT NULL DEFAULT 0,
    output_tokens      INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens  INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens INTEGER NOT NULL DEFAULT 0,
    web_searches       INTEGER NOT NULL DEFAULT 0,
    cost_usd           REAL NOT NULL DEFAULT 0,
    created_at         TEXT NOT NULL DEFAULT {_NOW}
);

CREATE INDEX IF NOT EXISTS idx_postings_company ON postings(company_id);
CREATE INDEX IF NOT EXISTS idx_events_application ON events(application_id);
CREATE INDEX IF NOT EXISTS idx_flags_status ON flags(status);

-- Rule 5: a blocked company can never get an application.
CREATE TRIGGER IF NOT EXISTS applications_not_blocked
BEFORE INSERT ON applications
WHEN EXISTS (SELECT 1 FROM company_blocks WHERE company_key = NEW.company_key)
BEGIN
    SELECT RAISE(ABORT, 'BLOCKED: company is on the do-not-apply or applied-before list');
END;

-- The application's key must be its company's key, so the UNIQUE rule cannot be dodged.
CREATE TRIGGER IF NOT EXISTS applications_key_matches_company
BEFORE INSERT ON applications
WHEN NEW.company_key IS NOT (SELECT company_key FROM companies WHERE id = NEW.company_id)
BEGIN
    SELECT RAISE(ABORT, 'MISMATCH: application company_key must equal its company''s key');
END;

CREATE TRIGGER IF NOT EXISTS applications_company_fixed
BEFORE UPDATE OF company_id, company_key ON applications
WHEN NEW.company_id IS NOT OLD.company_id OR NEW.company_key IS NOT OLD.company_key
BEGIN
    SELECT RAISE(ABORT, 'FIXED: the company of an application cannot change');
END;

CREATE TRIGGER IF NOT EXISTS companies_key_fixed
BEFORE UPDATE OF company_key ON companies
WHEN NEW.company_key IS NOT OLD.company_key
BEGIN
    SELECT RAISE(ABORT, 'FIXED: a company key cannot change');
END;

CREATE TRIGGER IF NOT EXISTS applications_created_event
AFTER INSERT ON applications
BEGIN
    INSERT INTO events (application_id, company_id, posting_id, event_type, new_status)
    VALUES (NEW.id, NEW.company_id, NEW.posting_id, 'created', NEW.status);
END;

CREATE TRIGGER IF NOT EXISTS applications_status_history
AFTER UPDATE OF status ON applications
WHEN NEW.status IS NOT OLD.status
BEGIN
    INSERT INTO events (application_id, company_id, posting_id, event_type, old_status, new_status)
    VALUES (NEW.id, NEW.company_id, NEW.posting_id, 'status_change', OLD.status, NEW.status);
    UPDATE applications SET updated_at = strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE id = NEW.id;
END;
"""


class AlreadyApplied(Exception):
    """The company already has an application (rule 5)."""


class CompanyBlocked(Exception):
    """The company is on a do-not-apply or applied-before list (rule 5)."""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def to_db_time(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def from_db_time(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


class SchemaOutdated(Exception):
    """The database file was made by an older version and needs a migration."""


def init_db(conn: sqlite3.Connection) -> None:
    has_meta = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'").fetchone()
    if has_meta:
        row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
        if row and int(row[0]) < SCHEMA_VERSION:
            raise SchemaOutdated(
                f"data/applier.db uses database layout {row[0]}; this version needs {SCHEMA_VERSION}. "
                "Ask Claude Code to migrate it (your records are kept)."
            )
    conn.executescript(SCHEMA)
    conn.execute(
        "INSERT INTO meta(key, value) VALUES ('schema_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (str(SCHEMA_VERSION),),
    )
    conn.commit()


def open_db(path: Path) -> sqlite3.Connection:
    conn = connect(path)
    init_db(conn)
    return conn


# ---------------------------------------------------------------- companies


def upsert_company(
    conn: sqlite3.Connection,
    name: str,
    key: str,
    *,
    website: str | None = None,
    careers_url: str | None = None,
    ats: str = "unknown",
    board_token: str | None = None,
    discovery_source: str = "companies_csv",
    discovery_status: str = "approved",
    notes: str | None = None,
) -> int:
    """Insert a company or fill in missing details on the existing one. Returns its id."""
    with conn:
        conn.execute(
            """
            INSERT INTO companies (name, company_key, website, careers_url, ats, board_token,
                                   discovery_source, discovery_status, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(company_key) DO UPDATE SET
                website     = COALESCE(excluded.website, companies.website),
                careers_url = COALESCE(excluded.careers_url, companies.careers_url),
                ats         = CASE WHEN excluded.ats != 'unknown' THEN excluded.ats ELSE companies.ats END,
                board_token = COALESCE(excluded.board_token, companies.board_token),
                notes       = COALESCE(excluded.notes, companies.notes),
                updated_at  = strftime('%Y-%m-%dT%H:%M:%SZ','now')
            """,
            (name, key, website, careers_url, ats, board_token, discovery_source, discovery_status, notes),
        )
    row = conn.execute("SELECT id FROM companies WHERE company_key = ?", (key,)).fetchone()
    return int(row["id"])


def is_blocked(conn: sqlite3.Connection, key: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM company_blocks WHERE company_key = ?", (key,)).fetchone()


def existing_application(conn: sqlite3.Connection, key: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM applications WHERE company_key = ?", (key,)).fetchone()


# ---------------------------------------------------------------- applications


def create_application(
    conn: sqlite3.Connection,
    company_id: int,
    role_title: str,
    *,
    posting_id: int | None = None,
    url: str | None = None,
    ats: str | None = None,
    score: int | None = None,
    mode: str | None = None,
    status: str = "queued",
) -> int:
    """Create the one application for a company. Raises AlreadyApplied or CompanyBlocked."""
    company = conn.execute("SELECT company_key FROM companies WHERE id = ?", (company_id,)).fetchone()
    if company is None:
        raise ValueError(f"no company with id {company_id}")
    try:
        with conn:
            cursor = conn.execute(
                """
                INSERT INTO applications (company_id, company_key, posting_id, role_title, url, ats,
                                          score, mode, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (company_id, company["company_key"], posting_id, role_title, url, ats, score, mode, status),
            )
    except sqlite3.IntegrityError as exc:
        message = str(exc)
        if "BLOCKED" in message:
            raise CompanyBlocked(company["company_key"]) from exc
        if "applications.company_key" in message:
            raise AlreadyApplied(company["company_key"]) from exc
        raise
    return int(cursor.lastrowid)


def set_status(conn: sqlite3.Connection, application_id: int, status: str, note: str | None = None) -> None:
    """Change an application's status. The database records the change and its time."""
    if status not in APPLICATION_STATUSES:
        raise ValueError(f"unknown status {status!r}")
    with conn:
        updated = conn.execute("UPDATE applications SET status = ? WHERE id = ?", (status, application_id))
        if updated.rowcount != 1:
            raise ValueError(f"no application with id {application_id}")
        if note:
            conn.execute(
                "INSERT INTO events (application_id, event_type, details) VALUES (?, 'note', ?)",
                (application_id, note),
            )


def mark_submitted(conn: sqlite3.Connection, application_id: int, status: str, when: datetime) -> None:
    if status not in SUBMITTED_STATUSES[:2]:
        raise ValueError("status must be submitted_unconfirmed or submitted")
    with conn:
        conn.execute(
            "UPDATE applications SET submitted_at = ? WHERE id = ?", (to_db_time(when), application_id)
        )
    set_status(conn, application_id, status)


def reset_company(conn: sqlite3.Connection, key: str, reason: str) -> bool:
    """Free a company for a new application (only when the user asks). The old row is archived."""
    row = existing_application(conn, key)
    if row is None:
        return False
    snapshot = json.dumps({k: row[k] for k in row.keys()})
    with conn:
        conn.execute(
            "INSERT INTO applications_archive (application_id, company_key, snapshot_json, reset_reason) "
            "VALUES (?, ?, ?, ?)",
            (row["id"], key, snapshot, reason),
        )
        conn.execute(
            "INSERT INTO events (application_id, company_id, event_type, old_status, details) "
            "VALUES (?, ?, 'company_reset', ?, ?)",
            (row["id"], row["company_id"], row["status"], reason),
        )
        conn.execute("UPDATE emails SET application_id = NULL WHERE application_id = ?", (row["id"],))
        conn.execute("DELETE FROM applications WHERE id = ?", (row["id"],))
    return True


def add_flag(
    conn: sqlite3.Connection,
    kind: str,
    message: str,
    *,
    severity: str = "normal",
    company_id: int | None = None,
    posting_id: int | None = None,
    application_id: int | None = None,
    email_id: int | None = None,
    due_at: str | None = None,
) -> int:
    """Record something that needs the user's attention. The same open flag is not repeated."""
    if kind not in FLAG_KINDS:
        raise ValueError(f"unknown flag kind {kind!r}")
    existing = conn.execute(
        "SELECT id FROM flags WHERE status = 'open' AND kind = ? AND message = ? AND company_id IS ? AND posting_id IS ?",
        (kind, message, company_id, posting_id),
    ).fetchone()
    if existing:
        return int(existing["id"])
    with conn:
        cursor = conn.execute(
            "INSERT INTO flags (severity, kind, message, company_id, posting_id, application_id, email_id, due_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (severity, kind, message, company_id, posting_id, application_id, email_id, due_at),
        )
    return int(cursor.lastrowid)


def start_run(conn: sqlite3.Connection, kind: str, mode: str | None = None) -> int:
    with conn:
        return int(conn.execute("INSERT INTO runs (kind, mode) VALUES (?, ?)", (kind, mode)).lastrowid)


def finish_run(conn: sqlite3.Connection, run_id: int, status: str, notes: str) -> None:
    with conn:
        conn.execute(
            "UPDATE runs SET status = ?, notes = ?, finished_at = strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE id = ?",
            (status, notes, run_id),
        )


# ---------------------------------------------------------------- counters for the safety gate


def _local_day_bounds(day: date, tz: ZoneInfo) -> tuple[str, str]:
    start = datetime(day.year, day.month, day.day, tzinfo=tz)
    end = start + timedelta(days=1)
    return to_db_time(start), to_db_time(end)


def submissions_on(conn: sqlite3.Connection, day: date, tz: ZoneInfo) -> int:
    """Applications submitted on a given local (New York) calendar day."""
    start, end = _local_day_bounds(day, tz)
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM applications WHERE submitted_at >= ? AND submitted_at < ?",
        (start, end),
    ).fetchone()
    return int(row["n"])


def submissions_total(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COUNT(*) AS n FROM applications WHERE submitted_at IS NOT NULL").fetchone()
    return int(row["n"])


def spend_on(conn: sqlite3.Connection, day: date, tz: ZoneInfo) -> float:
    start, end = _local_day_bounds(day, tz)
    row = conn.execute(
        "SELECT COALESCE(SUM(cost_usd), 0) AS total FROM api_usage WHERE created_at >= ? AND created_at < ?",
        (start, end),
    ).fetchone()
    return float(row["total"])


def table_counts(conn: sqlite3.Connection) -> dict[str, int]:
    counts = {}
    for table in ("companies", "postings", "applications", "highlights", "emails", "company_blocks"):
        counts[table] = int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    counts["open_flags"] = int(conn.execute("SELECT COUNT(*) FROM flags WHERE status = 'open'").fetchone()[0])
    return counts
