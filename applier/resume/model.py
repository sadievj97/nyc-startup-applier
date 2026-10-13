"""Schema for profile/master_resume.yaml and profile/resume_groups.yaml.

The master resume holds only content the user confirmed. Every bullet wording ("variant") is copied
verbatim from one of the user's own resume files and lists those files, so each claim can be traced.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import Field, ValidationError

from applier.config import ConfigError, StrictModel, _explain, _read_yaml

VariantStatus = Literal["ok", "disputed", "excluded"]


class Variant(StrictModel):
    id: str
    text: str
    sources: list[str]
    status: VariantStatus = "ok"
    conflict: str | None = None  # the conflict that disputes or excludes this wording


class Group(StrictModel):
    """One achievement (or skills line), with every true wording of it."""

    id: str
    label: str
    tags: list[str] = []
    default: bool = True  # used in the base resume
    variants: list[Variant] = []

    def usable(self) -> list[Variant]:
        return [v for v in self.variants if v.status == "ok"]

    def preferred(self) -> Variant | None:
        """The wording used in the most resume files; the shorter one on a tie."""
        usable = self.usable()
        return max(usable, key=lambda v: (len(v.sources), -len(v.text))) if usable else None


class Role(StrictModel):
    id: str
    organization: str
    organization_alternatives: list[str] = []
    descriptor: str | None = None  # shown in parentheses after the organization
    descriptor_alternatives: list[str] = []
    title: str
    title_alternatives: list[str] = []
    location: str
    dates: str
    default: bool = True
    may_omit: bool = False  # tailoring may leave this role out when it is not relevant
    pending: list[str] = []  # conflicts that must be resolved before these fields are final
    groups: list[Group] = []


class Education(StrictModel):
    id: str
    school: str
    note: str | None = None  # shown after the school name
    degree: str | None = None  # second line, in italics
    location: str
    dates: str
    pending: list[str] = []
    groups: list[Group] = []


class ConflictOption(StrictModel):
    label: str
    pattern: str | None = None  # wordings matching this belong to this option


class Conflict(StrictModel):
    """A fact that differs between resume versions, or a claim that needs the user's confirmation.

    A wording that matches any option's pattern touches this question. It stays in use only if it
    also matches the chosen option's pattern; until the user answers, it is not used at all.
    """

    id: str
    question: str
    options: dict[str, ConflictOption]
    groups: list[str] = []  # limit the patterns to these groups (empty: all groups)
    resolution: str | None = None  # the chosen option key
    note: str | None = None

    def chosen(self) -> ConflictOption | None:
        return self.options.get(self.resolution) if self.resolution else None

    def applies_to(self, group_id: str) -> bool:
        return not self.groups or group_id in self.groups

    def chosen(self) -> ConflictOption | None:
        return self.options.get(self.resolution) if self.resolution else None


class Meta(StrictModel):
    status: Literal["draft", "confirmed"] = "draft"
    confirmed_at: str | None = None
    content_hash: str | None = None
    sources: list[str] = []


class MasterResume(StrictModel):
    meta: Meta = Meta()
    name: str
    location: str | None = None  # shown in the contact line; None leaves it out
    pending: list[str] = []
    summaries: Group | None = None
    education: list[Education] = []
    experience: list[Role] = []
    leadership: list[Group] = []
    skills: list[Group] = []
    conflicts: list[Conflict] = []

    def all_groups(self) -> list[Group]:
        groups = [self.summaries] if self.summaries else []
        for entry in [*self.education, *self.experience]:
            groups.extend(entry.groups)
        return [*groups, *self.leadership, *self.skills]

    def content_hash(self) -> str:
        content = self.model_dump(exclude={"meta"}, mode="json")
        return hashlib.sha256(json.dumps(content, sort_keys=True).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- groups file (curation)


class Rule(StrictModel):
    """A paragraph belongs to a group when every given condition holds (each list is any-of)."""

    match: list[str] = []  # regular expressions, case-insensitive
    context: list[str] = []  # text in the heading the paragraph sat under
    section: list[str] = []  # text in the section heading (e.g. SKILLS, SUMMARY)

    def matches(self, text: str, contexts: list[str], sections: list[str]) -> bool:
        if self.match and not any(re.search(p, text, re.IGNORECASE) for p in self.match):
            return False
        if self.context and not any(c.lower() in ctx.lower() for c in self.context for ctx in contexts):
            return False
        if self.section and not any(s.lower() in sec.lower() for s in self.section for sec in sections):
            return False
        return True


STATED_PREFIX = "stated by you"


class AddedWording(StrictModel):
    """A fact the user stated in chat (not in any resume file), worded for the resume and confirmed by them."""

    group: str
    text: str
    stated: str  # date the user stated it, e.g. 2026-10-12

    @property
    def source(self) -> str:
        return f"{STATED_PREFIX} on {self.stated}"


class GroupSpec(StrictModel):
    id: str
    label: str
    tags: list[str] = []
    default: bool = True
    rules: list[Rule] = Field(min_length=1)


class RoleSpec(Role):
    groups: list[GroupSpec] = []  # type: ignore[assignment]


class EducationSpec(Education):
    groups: list[GroupSpec] = []  # type: ignore[assignment]


class GroupsFile(StrictModel):
    name: str
    location: str | None = None
    pending: list[str] = []
    summaries: GroupSpec | None = None
    education: list[EducationSpec] = []
    experience: list[RoleSpec] = []
    leadership: list[GroupSpec] = []
    skills: list[GroupSpec] = []
    ignore: list[Rule] = []  # paragraphs deliberately left out (contact lines, notes to recruiters)
    exclude_ids: dict[str, str] = {}  # wording id -> reason, for exclusions a pattern cannot express
    added: list[AddedWording] = []
    conflicts: list[Conflict] = []


def load_groups(path: Path) -> GroupsFile:
    try:
        return GroupsFile.model_validate(_read_yaml(path))
    except ValidationError as exc:
        raise _explain(path, exc) from None


def load_master(path: Path) -> MasterResume:
    try:
        return MasterResume.model_validate(_read_yaml(path))
    except ValidationError as exc:
        raise _explain(path, exc) from None


def save_master(path: Path, master: MasterResume) -> None:
    header = (
        "# Master resume: only content you confirmed. Built by `python -m applier profile build`\n"
        "# from profile/resume_groups.yaml and your resume files. Edit the groups file, not this one.\n"
    )
    path.write_text(
        header + yaml.safe_dump(master.model_dump(mode="json"), allow_unicode=True, sort_keys=False, width=1000),
        encoding="utf-8",
    )


def require_confirmed(path: Path) -> MasterResume:
    """The master resume, only if confirmed and unchanged since (truthfulness rule 1)."""
    master = load_master(path)
    if master.meta.status != "confirmed":
        raise ConfigError("The master resume is not confirmed yet. Review it, then run: profile confirm")
    if master.meta.content_hash != master.content_hash():
        raise ConfigError("The master resume changed after it was confirmed. Review it again, then run: profile confirm")
    return master
