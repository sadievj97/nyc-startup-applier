"""Rebuild the company_blocks table from do_not_apply.csv, applied_before.csv and the
do-not-apply folders. Runs at the start of every command, so edits take effect immediately.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from applier.companies import (
    ListedCompany,
    company_key,
    load_aliases,
    read_applied_before,
    read_company_folders,
    read_do_not_apply,
)
from applier.config import Config
from applier.paths import Paths

# When a company appears in more than one place, the strictest reason wins.
_PRIORITY = {"do_not_apply": 0, "applied_before": 1, "jobs_folder": 2}


@dataclass
class BlockReport:
    counts: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return sum(self.counts.values())

    @property
    def healthy(self) -> bool:
        return not self.errors


def sync_company_blocks(conn: sqlite3.Connection, paths: Paths, config: Config) -> BlockReport:
    report = BlockReport()
    aliases = load_aliases(paths.aliases_csv)

    sources: list[tuple[str, list[ListedCompany]]] = [
        ("do_not_apply", read_do_not_apply(paths.do_not_apply_csv)),
        ("applied_before", read_applied_before(paths.applied_before_csv)),
    ]
    for folder in config.do_not_apply_folders:
        try:
            sources.append(
                (
                    "jobs_folder",
                    read_company_folders(Path(folder), config.do_not_apply_folder_ignore, config.jobs_folder_highlight),
                )
            )
        except FileNotFoundError:
            report.errors.append(f"Cannot reach the do-not-apply folder {folder}")

    chosen: dict[str, tuple[str, ListedCompany]] = {}
    for reason, companies in sources:
        for company in companies:
            key = company_key(company.name, aliases)
            current = chosen.get(key)
            if current is None or _PRIORITY[reason] < _PRIORITY[current[0]]:
                chosen[key] = (reason, company)

    with conn:
        if report.healthy:
            conn.execute("DELETE FROM company_blocks")
        # If a folder is unreachable, keep the old blocks and only add new ones: never unblock by accident.
        conn.executemany(
            """
            INSERT INTO company_blocks (company_key, display_name, reason, highlight, source)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(company_key) DO UPDATE SET
                display_name = excluded.display_name, reason = excluded.reason,
                highlight = excluded.highlight, source = excluded.source,
                synced_at = strftime('%Y-%m-%dT%H:%M:%SZ','now')
            """,
            [
                (key, company.name, reason, int(company.highlight), company.source)
                for key, (reason, company) in chosen.items()
            ],
        )
    for reason, _company in chosen.values():
        report.counts[reason] = report.counts.get(reason, 0) + 1
    return report
