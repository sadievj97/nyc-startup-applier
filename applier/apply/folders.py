"""Your copy of every application, outside the project: one folder per job in config.applications_folder
(for example <Jobs folder>/Claude Applications), plus applications.csv there and in data/ (brief 7: export on every run).

Each folder holds the resume and cover letter that were sent, the answers, screenshots, the confirmation
page, and Application.md with the posting link, status, dates and the job description. The agent only adds
and updates its own files there; it never deletes anything.
"""

from __future__ import annotations

import csv
import re
import shutil
import sqlite3
from pathlib import Path

from applier.config import Config
from applier.db import from_db_time
from applier.paths import Paths

CSV_COLUMNS = ["id", "company", "role", "status", "score", "ats", "url", "created", "approved", "submitted",
               "needs_you", "folder"]


def folder_name(company: str, role: str) -> str:
    """'Company - Role', safe for Windows and short enough for long paths."""
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', " ", f"{company} - {role}")
    name = re.sub(r"\s+", " ", name).strip(" .")
    return name[:90].rstrip(" .")


def _rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT a.*, c.name AS company_name, p.title AS posting_title, p.url AS posting_url,
                  p.apply_url AS posting_apply_url, p.location AS posting_location,
                  p.description_text AS description, p.score_explanation AS score_explanation, p.pay_raw AS pay
           FROM applications a JOIN companies c ON c.id = a.company_id
           LEFT JOIN postings p ON p.id = a.posting_id ORDER BY a.id""").fetchall()


def _local(text: str | None, config: Config) -> str:
    """A database time (UTC) as New York time, e.g. 2026-10-12 17:06."""
    if not text:
        return ""
    return from_db_time(text).astimezone(config.submit_window.tz).strftime("%Y-%m-%d %H:%M")


def _copy_if_newer(source: Path, target: Path) -> bool:
    if not source.is_file():
        return False
    if target.exists() and target.stat().st_mtime >= source.stat().st_mtime:
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    return True


def _summary(row: sqlite3.Row, config: Config) -> str:
    lines = [f"# {row['company_name']}: {row['role_title']}", "",
             f"- Status: {row['status']}",
             f"- Posting: {row['posting_url'] or row['url'] or ''}",
             f"- Application page: {row['posting_apply_url'] or row['url'] or ''}",
             f"- Location: {row['posting_location'] or ''}",
             f"- Pay posted: {row['pay'] or 'not posted'}",
             f"- Score: {row['score'] if row['score'] is not None else ''}",
             f"- Prepared: {_local(row['created_at'], config)} (New York time)",
             f"- Approved: {_local(row['approved_at'], config)}",
             f"- Submitted: {_local(row['submitted_at'], config)}"]
    if row["needs_me_reason"] and row["status"] == "needs_me":
        lines.append(f"- Needs you: {row['needs_me_reason']}")
    if row["score_explanation"]:
        lines += ["", "## Why it scored", "", row["score_explanation"]]
    if row["description"]:
        lines += ["", "## Job description (saved when the agent applied)", "", row["description"]]
    return "\n".join(lines) + "\n"


def sync_folders(conn: sqlite3.Connection, config: Config, paths: Paths) -> list[str]:
    """Create or update one folder per application and write applications.csv. Returns notes for you."""
    rows = _rows(conn)
    notes: list[str] = []
    base = Path(config.applications_folder) if config.applications_folder else None
    if base is not None and not base.parent.is_dir():
        notes.append(f"cannot reach {base.parent}; application folders were not updated")
        base = None
    records = []
    for row in rows:
        folder = None
        if base is not None:
            folder = base / folder_name(row["company_name"], row["role_title"])
            folder.mkdir(parents=True, exist_ok=True)
            packet = Path(row["packet_dir"]) if row["packet_dir"] else None
            if packet and packet.is_dir():
                for source in [*packet.glob("*_Resume_*.pdf"), *packet.glob("*_Cover_Letter_*.pdf"),
                               packet / "cover_letter.txt", packet / "answers.md", packet / "confirmation.txt"]:
                    _copy_if_newer(source, folder / source.name)
                for shot in (packet / "screenshots").glob("*.png"):
                    _copy_if_newer(shot, folder / "screenshots" / shot.name)
            (folder / "Application.md").write_text(_summary(row, config), encoding="utf-8")
        records.append({
            "id": row["id"], "company": row["company_name"], "role": row["role_title"], "status": row["status"],
            "score": row["score"] if row["score"] is not None else "", "ats": row["ats"] or "",
            "url": row["posting_apply_url"] or row["url"] or "", "created": _local(row["created_at"], config),
            "approved": _local(row["approved_at"], config), "submitted": _local(row["submitted_at"], config),
            "needs_you": (row["needs_me_reason"] or "") if row["status"] == "needs_me" else "",
            "folder": str(folder) if folder else "",
        })
    targets = [paths.data_dir / "applications.csv"] + ([base / "applications.csv"] if base is not None else [])
    for target in targets:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("w", newline="", encoding="utf-8-sig") as handle:  # utf-8-sig opens cleanly in Excel
            writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS)
            writer.writeheader()
            writer.writerows(records)
    return notes
