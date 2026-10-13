"""Turn the extracted paragraphs plus the curated groups file into profile/master_resume.yaml.

Each wording goes to the first group whose rule matches (keyword rules before heading-only rules).
Wordings that touch an unresolved question are marked disputed; once the question is answered,
wordings that match a rejected option are excluded. Nothing is reworded here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from applier.resume.extract import ExtractedParagraph
from applier.resume.sources import normalize, paragraph_id
from applier.resume.model import (
    Conflict,
    Education,
    GroupsFile,
    GroupSpec,
    Group,
    MasterResume,
    Meta,
    Role,
    Variant,
)

# Only these paragraph kinds carry claims; headings are curated by hand into role fields.
_CLAIM_KINDS = {"bullet", "text"}


@dataclass
class BuildReport:
    unassigned: list[ExtractedParagraph] = field(default_factory=list)
    ignored: int = 0
    disputed: dict[str, int] = field(default_factory=dict)
    excluded: dict[str, int] = field(default_factory=dict)
    empty_groups: list[str] = field(default_factory=list)


def _specs_in_order(spec: GroupsFile) -> list[GroupSpec]:
    specs = [spec.summaries] if spec.summaries else []
    for entry in [*spec.education, *spec.experience]:
        specs.extend(entry.groups)
    return [*specs, *spec.leadership, *spec.skills]


def _variant_status(group_id: str, text: str, conflicts: list[Conflict]) -> tuple[str, str | None]:
    for conflict in conflicts:
        if not conflict.applies_to(group_id):
            continue
        hits = [key for key, opt in conflict.options.items() if opt.pattern and re.search(opt.pattern, text, re.I)]
        if not hits:
            continue
        if conflict.resolution is None:
            return "disputed", conflict.id
        if conflict.resolution not in hits:
            return "excluded", conflict.id
    return "ok", None


def build_master(spec: GroupsFile, paragraphs: list[ExtractedParagraph], sources: list[str]) -> tuple[MasterResume, BuildReport]:
    report = BuildReport()
    specs = _specs_in_order(spec)
    # Skills lines start with fixed labels ("Technical:"), so they are matched first; then keyword
    # rules; heading-only rules catch whatever is left under a role.
    others = [s for s in specs if s not in spec.skills]
    ordered_rules = (
        [(s, r) for s in spec.skills for r in s.rules]
        + [(s, r) for s in others for r in s.rules if r.match]
        + [(s, r) for s in others for r in s.rules if not r.match]
    )
    assigned: dict[str, list[ExtractedParagraph]] = {s.id: [] for s in specs}

    for para in paragraphs:
        # A summary sits under a SUMMARY heading, or directly under the contact line with no heading.
        is_summary = para.kind == "entry" and (
            not para.sections or any("SUMMARY" in sec.upper() for sec in para.sections)
        )
        if para.kind not in _CLAIM_KINDS and not is_summary:
            continue
        if any(rule.matches(para.text, para.contexts, para.sections) for rule in spec.ignore):
            report.ignored += 1
            continue
        if is_summary:
            if spec.summaries:
                assigned[spec.summaries.id].append(para)
            else:
                report.ignored += 1  # no summary group: the user chose not to use summary lines
            continue
        for group_spec, rule in ordered_rules:
            if rule.matches(para.text, para.contexts, para.sections):
                assigned[group_spec.id].append(para)
                break
        else:
            report.unassigned.append(para)

    # Facts the user stated in chat join their group like any other wording.
    for added in spec.added:
        if added.group not in assigned:
            raise ValueError(f"added wording names an unknown group: {added.group}")
        assigned[added.group].append(
            ExtractedParagraph(id=paragraph_id(added.text), text=normalize(added.text), kind="bullet",
                               sources=[added.source])
        )

    def make_group(group_spec: GroupSpec) -> Group:
        variants = []
        for para in sorted(assigned[group_spec.id], key=lambda p: -len(p.sources)):
            if para.id in spec.exclude_ids:
                status, conflict = "excluded", spec.exclude_ids[para.id]
            else:
                status, conflict = _variant_status(group_spec.id, para.text, spec.conflicts)
            if status == "disputed":
                report.disputed[conflict] = report.disputed.get(conflict, 0) + 1
            elif status == "excluded":
                report.excluded[conflict] = report.excluded.get(conflict, 0) + 1
            variants.append(Variant(id=para.id, text=para.text, sources=para.sources, status=status, conflict=conflict))
        if not variants:
            report.empty_groups.append(group_spec.id)
        return Group(id=group_spec.id, label=group_spec.label, tags=group_spec.tags, default=group_spec.default,
                     variants=variants)

    master = MasterResume(
        meta=Meta(status="draft", sources=sources),
        name=spec.name,
        location=spec.location,
        pending=spec.pending,
        summaries=make_group(spec.summaries) if spec.summaries else None,
        education=[
            Education(**e.model_dump(exclude={"groups"}), groups=[make_group(g) for g in e.groups])
            for e in spec.education
        ],
        experience=[
            Role(**r.model_dump(exclude={"groups"}), groups=[make_group(g) for g in r.groups])
            for r in spec.experience
        ],
        leadership=[make_group(g) for g in spec.leadership],
        skills=[make_group(g) for g in spec.skills],
        conflicts=spec.conflicts,
    )
    return master, report
