"""Checks run before the master resume can be confirmed, and before anything is rendered from it."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from applier.resume.model import STATED_PREFIX, MasterResume
from applier.resume.sources import normalize, read_source

# Rule 10: no contractions and no em dashes in anything written in the user's name.
_CONTRACTION = re.compile(r"\b\w+(n't|'re|'ve|'ll|'m|'d)\b|\b(it|that|there|what|he|she|let|who)'s\b", re.IGNORECASE)
_EM_DASH = "—"


def style_problems(text: str) -> list[str]:
    problems = []
    if _EM_DASH in text:
        problems.append("contains an em dash")
    found = _CONTRACTION.search(text.replace("’", "'"))
    if found:
        problems.append(f"contains a contraction ({found.group(0)})")
    return problems


@dataclass
class VerifyReport:
    problems: list[str] = field(default_factory=list)  # block confirmation
    notes: list[str] = field(default_factory=list)  # for information

    @property
    def ok(self) -> bool:
        return not self.problems


def open_questions(master: MasterResume) -> list[str]:
    resolved = {c.id for c in master.conflicts if c.resolution}
    known = {c.id for c in master.conflicts}
    referenced = set(master.pending)
    for entry in [*master.education, *master.experience]:
        referenced.update(entry.pending)
    for group in master.all_groups():
        referenced.update(v.conflict for v in group.variants if v.status == "disputed" and v.conflict)
    unknown = referenced - known
    return sorted((referenced - resolved) | unknown)


def verify_master(master: MasterResume, source_dir: Path) -> VerifyReport:
    report = VerifyReport()
    texts: dict[str, str | None] = {}

    def source_text(name: str) -> str | None:
        if name not in texts:
            path = source_dir / name
            texts[name] = read_source(path).normalized_text() if path.exists() else None
        return texts[name]

    for group in master.all_groups():
        for variant in group.variants:
            if not variant.sources:
                report.problems.append(f"{group.id}/{variant.id}: no source file listed")
            for name in variant.sources:
                if name.startswith(STATED_PREFIX):
                    continue  # stated by the user in chat; confirmed with the rest of the master resume
                text = source_text(name)
                if text is None:
                    report.problems.append(f"{group.id}/{variant.id}: source file missing: {name}")
                elif normalize(variant.text) not in text:
                    report.problems.append(f"{group.id}/{variant.id}: wording not found in {name}")
            if variant.status == "ok":
                for problem in style_problems(variant.text):
                    report.problems.append(f"{group.id}/{variant.id}: {problem}")
        if group.default and not group.usable():
            report.notes.append(f"{group.id}: no usable wording yet (waiting on an answer)")

    for entry in [*master.experience, *master.education]:
        fields = [getattr(entry, name, None) for name in ("organization", "school", "title", "descriptor", "note", "degree")]
        for value in filter(None, fields):
            for problem in style_problems(value):
                report.problems.append(f"{entry.id}: {problem} in {value!r}")

    for question in open_questions(master):
        report.problems.append(f"question {question} is not answered yet")
    return report
