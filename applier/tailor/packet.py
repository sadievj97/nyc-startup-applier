"""One application packet: tailored resume PDF, optional cover letter, and the fact-check report.

packets/<Company>_<posting id>/ holds:
  <First>_<Last>_Resume_<Company>.pdf   the resume to upload
  tailored.yaml                           every bullet with the confirmed wording it came from
  cover_letter.txt, cover_sources.json    only when a cover letter is needed
  factcheck.md, factcheck.json            the gate's verdict; a failed gate blocks submission
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from applier import db
from applier.config import Config
from applier.jobs.http import PoliteClient
from applier.jobs.rules import hidden_instructions_note
from applier.paths import Paths
from applier.profile import load_profile
from applier.resume.model import require_confirmed
from applier.resume.render import TooLong, render_pdf, resume_filename
from applier.tailor import cover, factcheck, select

FAKE_METRIC = ", growing revenue 47% in six months"


def safe_name(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_")


def packet_dir(paths: Paths, company: str, posting_id: int) -> Path:
    return paths.root / "packets" / f"{safe_name(company)}_{posting_id}"


@dataclass
class PacketResult:
    directory: Path
    resume: Path | None = None
    pages: int = 0
    report: factcheck.GateReport | None = None
    notes: list[str] = field(default_factory=list)
    cover_letter: cover.CoverLetter | None = None
    cost_usd: float = 0.0


def _load_posting(conn: sqlite3.Connection, posting_id: int) -> sqlite3.Row:
    row = conn.execute(
        """SELECT p.*, c.name AS company_name, c.website AS company_website, c.id AS company_row
           FROM postings p JOIN companies c ON c.id = p.company_id WHERE p.id = ?""", (posting_id,)).fetchone()
    if row is None:
        raise ValueError(f"no posting #{posting_id}")
    return row


def _render(paths: Paths, config: Config, tailored: select.TailoredResume, master, profile, out: Path):
    """Render, dropping the least important bullet until it fits the page limit."""
    max_pages = 2 if config.allow_two_pages else 1
    for _ in range(12):
        try:
            return render_pdf(select.build_document(tailored, master, profile), out, paths.browsers_dir, max_pages)
        except TooLong:
            dropped = select.drop_one_bullet(tailored)
            if dropped is None:
                raise
            tailored.dropped_to_fit.append(dropped)
    raise TooLong("could not fit the resume on the page")


def plant_fake_metric(tailored: select.TailoredResume) -> str:
    """For the acceptance test: add an invented number to the first experience bullet."""
    bullet = tailored.roles[0].bullets[0]
    bullet.text = bullet.text.rstrip(".") + FAKE_METRIC
    return bullet.text


def _save(directory: Path, tailored, report: factcheck.GateReport, letter: cover.CoverLetter | None) -> None:
    select.save_tailored(directory / "tailored.yaml", tailored)
    (directory / "factcheck.md").write_text(report.to_markdown(), encoding="utf-8")
    (directory / "factcheck.json").write_text(json.dumps(report.to_dict(), indent=1), encoding="utf-8")
    if letter is not None:
        (directory / "cover_letter.txt").write_text(letter.text + "\n", encoding="utf-8")
        (directory / "cover_sources.json").write_text(json.dumps(letter.to_dict(), indent=1), encoding="utf-8")


def _style(paths: Paths) -> str:
    style_file = paths.profile_dir / "writing_style.md"
    return style_file.read_text(encoding="utf-8") if style_file.exists() else ""


def _gate(client, conn, config, tailored, master, profile, letter, posting_row, run_id,
          style: str = "") -> factcheck.GateReport:
    extra_items = []
    if letter:
        context = cover.letter_context(master, style, dict(posting_row), letter.applicant_sources)
        extra_items = cover.cover_check_items(letter, profile, context)
    extra_texts = {"cover_letter": letter.text} if letter else {}
    report = factcheck.run_gate(client, conn, config, tailored, master, extra_items, extra_texts, run_id)
    if letter and letter.problems:
        report.findings += [factcheck.Finding("cover letter", "", p, "code") for p in letter.problems]
        report.passed = False
    return report


def _flag_if_blocked(conn: sqlite3.Connection, report: factcheck.GateReport, posting_row) -> None:
    """Raise a flag for the final verdict only (not for a draft that a revision fixed)."""
    if not report.passed:
        db.add_flag(conn, "fact_check_failed",
                    f"{posting_row['company_name']} | {posting_row['title']}: {len(report.findings)} unsupported "
                    f"claim(s); see the packet's factcheck.md", posting_id=posting_row["id"],
                    company_id=posting_row["company_row"])


def build_packet(client: Any, conn: sqlite3.Connection, config: Config, paths: Paths, posting_id: int, *,
                 cover_letter: bool = False, plant: bool = False, http: PoliteClient | None = None,
                 run_id: int | None = None) -> PacketResult:
    row = _load_posting(conn, posting_id)
    master = require_confirmed(paths.master_file)
    profile = load_profile(paths.profile_file)
    style = _style(paths)
    directory = packet_dir(paths, row["company_name"], posting_id)
    directory.mkdir(parents=True, exist_ok=True)
    result = PacketResult(directory)
    posting = dict(row)
    meta = {"id": posting_id, "company": row["company_name"], "title": row["title"], "url": row["url"],
            "apply_url": row["apply_url"]}

    warning = hidden_instructions_note(row["description_text"] or "")
    if warning:
        result.notes.append(f"posting seems to contain instructions aimed at AI tools (ignored): {warning}")
        db.add_flag(conn, "other", f"{row['company_name']} | {row['title']}: posting contains text aimed at AI "
                    f"applicants (ignored): {warning[:150]}", posting_id=posting_id)

    selection, usd = select.request_selection(client, conn, config, master, style, posting, row["company_name"],
                                              warning, run_id)
    result.cost_usd += usd
    (directory / "selection.json").write_text(json.dumps(selection, indent=1), encoding="utf-8")
    tailored, problems = select.to_tailored(selection, master, meta)
    result.notes += [f"model answer adjusted: {p}" for p in problems]
    if plant:
        result.notes.append(f"planted for the acceptance test: {plant_fake_metric(tailored)}")

    out = directory / resume_filename(profile.name.full, row["company_name"])
    rendered = _render(paths, config, tailored, master, profile, out)
    result.resume, result.pages = rendered.path, rendered.pages
    result.notes += [f"left out to fit one page: {d}" for d in tailored.dropped_to_fit]

    letter = None
    pages: list[cover.Page] = []
    if cover_letter or config.cover_letter == "always":
        pages = cover.company_pages(http or PoliteClient(), row["company_website"])
        if row["description_text"]:  # the company's own words, on its own careers board
            pages.append(cover.Page(row["url"] or row["apply_url"] or "posting", row["description_text"][:6000]))
        letter = cover.write_cover_letter(client, conn, config, master, profile, style, posting,
                                          row["company_name"], pages, run_id)
        result.cost_usd += letter.cost_usd
        result.cover_letter = letter

    result.report = _gate(client, conn, config, tailored, master, profile, letter, row, run_id, _style(paths))
    result.cost_usd += result.report.cost_usd

    # If only cover-letter sentences were flagged, the writer gets one chance to fix exactly those.
    flagged = [(f.text, f.problem) for f in result.report.findings]
    if letter and flagged and all(f.where == "cover letter" and f.text for f in result.report.findings):
        revised = cover.write_cover_letter(client, conn, config, master, profile, style, posting,
                                           row["company_name"], pages, run_id, previous=letter, flagged=flagged)
        result.cost_usd += revised.cost_usd
        report = _gate(client, conn, config, tailored, master, profile, revised, row, run_id, _style(paths))
        result.cost_usd += report.cost_usd
        result.notes.append("cover letter revised once after the fact-check flagged "
                            f"{len(flagged)} sentence(s); the revision {'passed' if report.passed else 'was also blocked'}")
        letter, result.cover_letter, result.report = revised, revised, report
    _flag_if_blocked(conn, result.report, row)
    _save(directory, tailored, result.report, letter)
    return result


def recheck_packet(client: Any, conn: sqlite3.Connection, config: Config, paths: Paths, posting_id: int,
                   run_id: int | None = None) -> PacketResult:
    """Re-run the gate after you edited tailored.yaml or cover_letter.txt, and re-render the PDF."""
    row = _load_posting(conn, posting_id)
    master = require_confirmed(paths.master_file)
    profile = load_profile(paths.profile_file)
    directory = packet_dir(paths, row["company_name"], posting_id)
    if not (directory / "tailored.yaml").exists():
        raise ValueError(f"no packet for posting #{posting_id}; run: tailor {posting_id}")
    tailored = select.load_tailored(directory / "tailored.yaml")
    letter = None
    if (directory / "cover_sources.json").exists():
        letter = cover.CoverLetter(**json.loads((directory / "cover_sources.json").read_text(encoding="utf-8")))
        letter.text = (directory / "cover_letter.txt").read_text(encoding="utf-8").strip()
        letter.problems = [p for p in letter.problems if "words" not in p]
        if letter.words > cover.MAX_WORDS:
            letter.problems.append(f"{letter.words} words; the limit is {cover.MAX_WORDS}")
    result = PacketResult(directory, cover_letter=letter)
    out = directory / resume_filename(profile.name.full, row["company_name"])
    rendered = _render(paths, config, tailored, master, profile, out)
    result.resume, result.pages = rendered.path, rendered.pages
    result.report = _gate(client, conn, config, tailored, master, profile, letter, row, run_id, _style(paths))
    result.cost_usd = result.report.cost_usd
    _flag_if_blocked(conn, result.report, row)
    _save(directory, tailored, result.report, letter)
    return result


def packet_passed(directory: Path) -> bool:
    """True only if the gate passed for exactly the content now in the packet (Phase 5 checks this)."""
    report_file, tailored_file = directory / "factcheck.json", directory / "tailored.yaml"
    if not (report_file.exists() and tailored_file.exists()):
        return False
    report = json.loads(report_file.read_text(encoding="utf-8"))
    tailored = select.load_tailored(tailored_file)
    extra = {}
    if (directory / "cover_letter.txt").exists():
        extra["cover_letter"] = (directory / "cover_letter.txt").read_text(encoding="utf-8").strip()
    return bool(report.get("passed")) and report.get("content_hash") == factcheck.content_hash(tailored, extra)
