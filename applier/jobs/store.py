"""Save companies and postings, score them, and pick at most one posting per company (brief 3 and 4)."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta

from applier import db
from applier.companies import _read_csv, company_key
from applier.config import Config, Targets
from applier.jobs import lca, research
from applier.jobs.ats import Posting
from applier.jobs.scoring import score_posting
from applier.paths import Paths

_ATS_VALUES = set(db.ATS_TYPES)


def sync_companies_csv(conn: sqlite3.Connection, paths: Paths, aliases: dict[str, str]) -> int:
    """Add or update the companies you listed in config/companies.csv."""
    rows = _read_csv(paths.companies_csv, ["name"])
    for row in rows:
        if not row.get("name"):
            continue
        ats = (row.get("ats") or "unknown").lower()
        db.upsert_company(
            conn,
            row["name"],
            company_key(row["name"], aliases),
            website=row.get("website") or None,
            careers_url=row.get("careers_url") or None,
            ats=ats if ats in _ATS_VALUES else "manual",
            board_token=row.get("board_token") or None,
            discovery_source="companies_csv",
            notes=row.get("notes") or None,
        )
    return len(rows)


@dataclass
class FetchCounts:
    new: int = 0
    updated: int = 0
    closed: int = 0

    @property
    def total(self) -> int:
        return self.new + self.updated


_FIELDS = [
    "title", "department", "team", "location", "workplace_type", "employment_type", "url", "apply_url",
    "pay_min", "pay_max", "pay_currency", "pay_interval", "pay_raw", "published_at", "source_updated_at",
    "description_raw", "description_text",
]


def upsert_postings(conn: sqlite3.Connection, company_id: int, ats: str, postings: list[Posting],
                    now: datetime) -> FetchCounts:
    counts = FetchCounts()
    stamp = db.to_db_time(now)
    existing = {
        row["ats_posting_id"]: row
        for row in conn.execute(
            "SELECT id, ats_posting_id, closed_at FROM postings WHERE company_id = ? AND ats = ?", (company_id, ats)
        )
    }
    seen = set()
    with conn:
        for posting in postings:
            seen.add(posting.ats_posting_id)
            values = {name: getattr(posting, name) for name in _FIELDS}
            values["other_locations"] = json.dumps(posting.other_locations)
            values["extra_json"] = json.dumps(posting.extra, default=str)
            values["is_listed"] = int(posting.is_listed)
            if posting.ats_posting_id in existing:
                assignments = ", ".join(f"{k} = ?" for k in values)
                conn.execute(
                    f"UPDATE postings SET {assignments}, last_seen = ?, closed_at = NULL WHERE id = ?",
                    [*values.values(), stamp, existing[posting.ats_posting_id]["id"]],
                )
                counts.updated += 1
            else:
                columns = ["company_id", "ats", "ats_posting_id", *values, "first_seen", "last_seen"]
                conn.execute(
                    f"INSERT INTO postings ({', '.join(columns)}) VALUES ({', '.join('?' * len(columns))})",
                    [company_id, ats, posting.ats_posting_id, *values.values(), stamp, stamp],
                )
                counts.new += 1
        for posting_id, row in existing.items():
            if posting_id not in seen and row["closed_at"] is None:
                conn.execute(
                    "UPDATE postings SET closed_at = ?, decision = 'closed', decision_reason = ? WHERE id = ?",
                    (stamp, "no longer on the company's job board", row["id"]),
                )
                counts.closed += 1
    return counts


def score_all(conn: sqlite3.Connection, targets: Targets, config: Config) -> dict[str, int]:
    """Score every open posting and record the filter decision and its explanation."""
    tally: dict[str, int] = {}
    rows = conn.execute(
        """SELECT p.*, c.industry AS company_industry, c.name AS company_name, c.company_key AS company_key
           FROM postings p JOIN companies c ON c.id = p.company_id WHERE p.closed_at IS NULL"""
    ).fetchall()
    source = lca.loaded_source(conn) if targets.sponsorship.show_lca_history else None
    lca_notes: dict[str, str | None] = {}
    facts_by_company: dict[int, dict] = {}
    with conn:
        for row in rows:
            posting = dict(row)
            key = posting["company_key"]
            if source is not None and key not in lca_notes:
                lca_notes[key] = lca.history_note(lca.lookup(conn, key), source["source_file"])
            if posting["company_id"] not in facts_by_company:
                facts_by_company[posting["company_id"]] = research.load_facts(conn, posting["company_id"])
            company = {"industry": posting["company_industry"], "lca_note": lca_notes.get(key),
                       "facts": facts_by_company[posting["company_id"]]}
            result = score_posting(posting, company, targets, config)
            conn.execute(
                """UPDATE postings SET score = ?, score_explanation = ?, decision = ?, decision_reason = ?,
                   required_years = ?, sponsorship_note = ?, ai_policy_note = ? WHERE id = ?""",
                (result.score, result.explanation(), result.decision,
                 result.filters[0] if result.filters else None, result.required_years,
                 result.sponsorship_note, result.ai_policy_note, row["id"]),
            )
            tally[result.decision] = tally.get(result.decision, 0) + 1
    return tally


# Decisions that mean a posting passed every filter.
ELIGIBLE = ("eligible", "chosen", "skipped_one_per_company", "below_min_score", "blocked_company")


def choose_per_company(conn: sqlite3.Connection, config: Config) -> dict[str, int]:
    """At most one posting per company: the highest-scoring eligible one at or above min_score."""
    tally: dict[str, int] = {}

    def mark(posting_id: int, decision: str, reason: str | None) -> None:
        conn.execute("UPDATE postings SET decision = ?, decision_reason = COALESCE(?, decision_reason) WHERE id = ?",
                     (decision, reason, posting_id))
        tally[decision] = tally.get(decision, 0) + 1

    with conn:
        for company in conn.execute("SELECT id, company_key, name FROM companies").fetchall():
            open_rows = conn.execute(
                "SELECT id, score, decision, published_at FROM postings WHERE company_id = ? AND closed_at IS NULL",
                (company["id"],),
            ).fetchall()
            if not open_rows:
                continue
            block = db.is_blocked(conn, company["company_key"])
            applied = db.existing_application(conn, company["company_key"])
            if block or applied:
                why = (f"never apply: {block['reason'].replace('_', ' ')}" if block
                       else f"already applied (application #{applied['id']})")
                for row in open_rows:  # filtered postings keep their filter reason (highlights need it)
                    if row["decision"] in ELIGIBLE:
                        mark(row["id"], "blocked_company", why)
                continue
            skipped = {r[0] for r in conn.execute(
                "SELECT posting_id FROM posting_skips WHERE posting_id IN (SELECT id FROM postings WHERE company_id = ?)",
                (company["id"],))}
            for row in open_rows:
                if row["id"] in skipped:
                    mark(row["id"], "manual", "you chose to skip this posting")
            open_rows = [r for r in open_rows if r["id"] not in skipped]
            eligible = sorted(
                (r for r in open_rows if r["decision"] in ("eligible", "chosen", "skipped_one_per_company",
                                                           "below_min_score")),
                key=lambda r: (r["score"] or 0, r["published_at"] or ""),
                reverse=True,
            )
            for index, row in enumerate(eligible):
                if (row["score"] or 0) < config.min_score:
                    mark(row["id"], "below_min_score", f"score {row['score']} is below {config.min_score}")
                elif index == 0:
                    mark(row["id"], "chosen", "best eligible posting at this company")
                else:
                    mark(row["id"], "skipped_one_per_company", f"a higher-scoring posting at {company['name']} was chosen")
    return tally


def is_new(first_seen: str, config: Config, now: datetime) -> bool:
    return db.from_db_time(first_seen) >= now - timedelta(days=config.new_window_days)


def posting_to_dict(posting: Posting) -> dict:
    data = asdict(posting)
    data["other_locations"] = json.dumps(posting.other_locations)
    return data
