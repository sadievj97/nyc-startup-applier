"""The finding-jobs steps in order: companies -> HN discovery -> board detection -> fetch -> score -> choose."""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime

from applier import db
from applier.companies import company_key
from applier.config import Config, Targets
from applier.jobs.ats import FETCHABLE, fetch_board, fetch_greenhouse_details
from applier.jobs.detect import BoardRef, detect
from applier.jobs.hn import latest_thread, nyc_posts
from applier.jobs.http import FetchError, PoliteClient
from applier.jobs.rules import is_nyc
from applier.jobs.scoring import has_term
from applier.jobs.store import FetchCounts, upsert_postings

log = logging.getLogger(__name__)


@dataclass
class HNResult:
    thread: str | None = None
    companies: list[str] = field(default_factory=list)


def discover_hn(conn: sqlite3.Connection, client: PoliteClient, aliases: dict[str, str],
                month: str | None = None) -> HNResult:
    thread = latest_thread(client, month)
    if thread is None:
        return HNResult()
    result = HNResult(thread=thread.title)
    for post in nyc_posts(client, thread):
        board = next((b for b in post.boards if b.ats in db.ATS_TYPES), None)
        db.upsert_company(
            conn,
            post.company,
            company_key(post.company, aliases),
            website=post.website,
            ats=board.ats if board else "unknown",
            board_token=board.token if board else None,
            discovery_source="hn",
            notes=f"HN {thread.month} (item {post.id}): {post.header[:200]}",
        )
        result.companies.append(post.company)
    return result


def detect_missing(conn: sqlite3.Connection, client: PoliteClient, limit: int = 50) -> list[tuple[str, BoardRef | None]]:
    rows = conn.execute(
        "SELECT * FROM companies WHERE ats = 'unknown' AND discovery_status = 'approved' ORDER BY id LIMIT ?",
        (limit,),
    ).fetchall()
    results = []
    for row in rows:
        ref = detect(client, row["name"], row["website"], row["careers_url"], hint_text=row["notes"] or "")
        with conn:
            if ref is None:
                conn.execute("UPDATE companies SET ats = 'manual', updated_at = strftime('%Y-%m-%dT%H:%M:%SZ','now'), "
                             "notes = COALESCE(notes, '') || ' [no supported job board found]' WHERE id = ?",
                             (row["id"],))
            else:
                ats = ref.ats if ref.ats in db.ATS_TYPES else "manual"
                conn.execute("UPDATE companies SET ats = ?, board_token = ?, "
                             "updated_at = strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE id = ?",
                             (ats, ref.token, row["id"]))
        results.append((row["name"], ref))
    return results


def _greenhouse_candidate(posting, targets: Targets) -> bool:
    places = [posting.location or "", *posting.other_locations]
    family = any(has_term(posting.title, t) for terms in targets.role_families.values() for t in terms)
    return family and any(is_nyc(p) for p in places)


@dataclass
class FetchSummary:
    boards: int = 0
    counts: FetchCounts = field(default_factory=FetchCounts)
    missing: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def fetch_all(conn: sqlite3.Connection, client: PoliteClient, targets: Targets, now: datetime,
              only: str | None = None) -> FetchSummary:
    summary = FetchSummary()
    query = ("SELECT * FROM companies WHERE board_token IS NOT NULL AND discovery_status = 'approved' "
             f"AND ats IN ({', '.join('?' * len(FETCHABLE))})")
    params: list = list(FETCHABLE)
    if only:
        query += " AND company_key = ?"
        params.append(company_key(only))
    for company in conn.execute(query, params).fetchall():
        try:
            postings = fetch_board(client, company["ats"], company["board_token"])
            if postings is None:
                summary.missing.append(company["name"])
                db.add_flag(conn, "api_error", f"{company['name']}: {company['ats']} board "
                            f"'{company['board_token']}' was not found", company_id=company["id"])
                continue
            if company["ats"] == "greenhouse":
                for posting in postings:
                    if _greenhouse_candidate(posting, targets):
                        fetch_greenhouse_details(client, company["board_token"], posting)
        except FetchError as exc:
            summary.errors.append(f"{company['name']}: {exc}")
            db.add_flag(conn, "api_error", f"{company['name']}: could not read job board ({exc})",
                        company_id=company["id"])
            continue
        counts = upsert_postings(conn, company["id"], company["ats"], postings, now)
        summary.boards += 1
        summary.counts.new += counts.new
        summary.counts.updated += counts.updated
        summary.counts.closed += counts.closed
    return summary
