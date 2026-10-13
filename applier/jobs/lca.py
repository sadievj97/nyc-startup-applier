"""Sponsorship history from the Department of Labor LCA disclosure file (brief 4).

Only employer-level counts are kept. The file also names employer contacts, attorneys and preparers;
none of that is read into the database (no personal contact details, brief 4b).
"""

from __future__ import annotations

import sqlite3
import xml.etree.ElementTree as ET
import zipfile
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from applier.companies import company_key
from applier.jobs.rules import is_nyc

_NEEDED = ("CASE_STATUS", "DECISION_DATE", "VISA_CLASS", "JOB_TITLE", "EMPLOYER_NAME", "TRADE_NAME_DBA",
           "WORKSITE_CITY", "WORKSITE_STATE")

SCHEMA = """
CREATE TABLE IF NOT EXISTS lca_employers (
    employer_key   TEXT PRIMARY KEY,
    employer_name  TEXT NOT NULL,
    filings        INTEGER NOT NULL,
    certified      INTEGER NOT NULL,
    h1b            INTEGER NOT NULL,
    nyc_filings    INTEGER NOT NULL,
    latest_date    TEXT,
    top_titles     TEXT,
    source_file    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS lca_sources (
    source_file TEXT PRIMARY KEY,
    file_size   INTEGER NOT NULL,
    rows_read   INTEGER NOT NULL,
    loaded_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
"""


@dataclass
class EmployerSummary:
    name: str
    filings: int = 0
    certified: int = 0
    h1b: int = 0
    nyc: int = 0
    latest: str = ""
    titles: Counter = field(default_factory=Counter)


_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


def _excel_date(value: str | None) -> str:
    """Excel stores dates as day numbers; turn them into YYYY-MM-DD."""
    if not value:
        return ""
    try:
        return (datetime(1899, 12, 30) + timedelta(days=float(value))).strftime("%Y-%m-%d")
    except ValueError:
        return str(value)[:10]


def iter_columns(path: Path, wanted: tuple[str, ...]):
    """Stream rows of the first sheet, keeping only the wanted columns. Much faster than openpyxl
    on the million-row LCA file, because the other 90 columns are never turned into objects."""
    with zipfile.ZipFile(path) as archive:
        strings: list[str] = []
        if "xl/sharedStrings.xml" in archive.namelist():
            with archive.open("xl/sharedStrings.xml") as handle:
                for _, element in ET.iterparse(handle):
                    if element.tag == _NS + "si":
                        strings.append("".join(t.text or "" for t in element.iter(_NS + "t")))
                        element.clear()
        sheet = next(n for n in sorted(archive.namelist()) if n.startswith("xl/worksheets/sheet"))
        columns: dict[str, str] | None = None
        with archive.open(sheet) as handle:
            for _, element in ET.iterparse(handle):
                if element.tag != _NS + "row":
                    continue
                values: dict[str, str | None] = {}
                for cell in element.iter(_NS + "c"):
                    letters = "".join(ch for ch in cell.get("r", "") if ch.isalpha())
                    if columns is not None and letters not in columns:
                        continue
                    kind, node = cell.get("t"), cell.find(_NS + "v")
                    if kind == "s" and node is not None:
                        values[letters] = strings[int(node.text)]
                    elif kind == "inlineStr":
                        values[letters] = "".join(t.text or "" for t in cell.iter(_NS + "t"))
                    else:
                        values[letters] = node.text if node is not None else None
                element.clear()
                if columns is None:
                    names = {str(v or "").strip().upper(): letters for letters, v in values.items()}
                    missing = [c for c in wanted if c not in names]
                    if missing:
                        raise ValueError(f"{path.name} is missing column(s): {', '.join(missing)}")
                    columns = {names[c]: c for c in wanted}
                    continue
                yield {name: values.get(letters) for letters, name in columns.items()}


def summarize(path: Path, progress: Callable[[int], None] | None = None) -> tuple[dict[str, EmployerSummary], int]:
    summaries: dict[str, EmployerSummary] = {}
    keys: dict[str, str | None] = {}  # employer names repeat thousands of times

    def key_for(display: str) -> str | None:
        if display not in keys:
            try:
                keys[display] = company_key(display)
            except ValueError:
                keys[display] = None
        return keys[display]

    count = 0
    for record in iter_columns(path, _NEEDED):
        count += 1
        if progress and count % 100_000 == 0:
            progress(count)
        name = (record["EMPLOYER_NAME"] or "").strip()
        if not name:
            continue
        names = {name}
        if record["TRADE_NAME_DBA"]:
            names.add(record["TRADE_NAME_DBA"].strip())
        status = (record["CASE_STATUS"] or "").lower()
        decided = _excel_date(record["DECISION_DATE"])
        state = (record["WORKSITE_STATE"] or "").upper()
        nyc = state in ("NY", "NEW YORK") and is_nyc(record["WORKSITE_CITY"] or "")
        h1b = (record["VISA_CLASS"] or "").upper() == "H-1B"
        title = (record["JOB_TITLE"] or "").strip().title()
        # The legal name and trade name often normalize to the same company: count the filing once.
        for key in {k for k in map(key_for, names) if k}:
            summary = summaries.setdefault(key, EmployerSummary(name=name))
            summary.filings += 1
            summary.certified += status.startswith("certified")
            summary.h1b += h1b
            summary.nyc += nyc
            summary.latest = max(summary.latest, decided)
            if title:
                summary.titles[title] += 1
    return summaries, count


def save(conn: sqlite3.Connection, summaries: dict[str, EmployerSummary], path: Path, rows: int) -> None:
    conn.executescript(SCHEMA)
    with conn:
        conn.execute("DELETE FROM lca_employers WHERE source_file = ?", (path.name,))
        conn.executemany(
            "INSERT OR REPLACE INTO lca_employers VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (key, s.name, s.filings, s.certified, s.h1b, s.nyc, s.latest or None,
                 "; ".join(t for t, _ in s.titles.most_common(3)), path.name)
                for key, s in summaries.items()
            ],
        )
        conn.execute("INSERT OR REPLACE INTO lca_sources (source_file, file_size, rows_read) VALUES (?, ?, ?)",
                     (path.name, path.stat().st_size, rows))


def loaded_source(conn: sqlite3.Connection) -> sqlite3.Row | None:
    conn.executescript(SCHEMA)
    return conn.execute("SELECT * FROM lca_sources ORDER BY loaded_at DESC LIMIT 1").fetchone()


def lookup(conn: sqlite3.Connection, key: str) -> sqlite3.Row | None:
    conn.executescript(SCHEMA)
    return conn.execute("SELECT * FROM lca_employers WHERE employer_key = ?", (key,)).fetchone()


def history_note(row: sqlite3.Row | dict | None, source: str | None) -> str | None:
    if source is None:
        return None
    period = source.replace("LCA_Disclosure_Data_", "").replace(".xlsx", "").replace("_", " ")
    if row is None:
        return (f"Sponsorship history: no LCA filings found under this name in {period} "
                "(a new or small company may still sponsor; the legal name may differ)")
    return (f"Sponsorship history ({period}): {row['filings']} LCA filing(s) by {row['employer_name']}, "
            f"{row['certified']} certified, {row['nyc_filings']} for New York worksites; "
            f"common titles: {row['top_titles'] or 'n/a'}")
