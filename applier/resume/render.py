"""Render a resume from the master resume: HTML template -> PDF with Playwright Chromium (brief 5).

The renderer only places confirmed wordings; it never writes new text. A draft (unconfirmed)
master can be previewed: such PDFs carry a DRAFT note and highlight wordings still in question.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape
from pypdf import PdfReader

from applier.profile import Profile
from applier.resume.model import Group, MasterResume, Variant
from applier.resume.verify import style_problems

TEMPLATES = Path(__file__).parent / "templates"
_LABEL = re.compile(r"^([^:]{2,80}):\s+(.+)$")
# Font sizes and margins tried, in order, until the resume fits the page limit.
FIT_STEPS = [(10.5, 0.45), (10.25, 0.42), (10.0, 0.4), (9.75, 0.38)]


class RenderError(Exception):
    pass


class TooLong(RenderError):
    """The resume does not fit the page limit even at the smallest font step."""


@dataclass
class Bullet:
    text: str
    label: str | None = None
    disputed: bool = False


@dataclass
class Entry:
    heading_left: str | None = None
    heading_rest: str | None = None
    heading_right: str | None = None
    subtitle: str | None = None
    bullets: list[Bullet] = field(default_factory=list)


@dataclass
class Section:
    title: str
    entries: list[Entry] = field(default_factory=list)


@dataclass
class ResumeDoc:
    name: str
    contact_line: str
    sections: list[Section]
    summary: str | None = None
    draft_note: str | None = None
    font_pt: float = 10.5
    margin_in: float = 0.45

    def all_text(self) -> list[str]:
        texts = [self.name, self.contact_line] + ([self.summary] if self.summary else [])
        for section in self.sections:
            for entry in section.entries:
                texts += [t for t in (entry.heading_left, entry.heading_rest, entry.subtitle) if t]
                texts += [(f"{b.label}: " if b.label else "") + b.text for b in entry.bullets]
        return texts


def short_link(url: str) -> str:
    return re.sub(r"^https?://(www\.)?", "", url).rstrip("/")


def contact_line(master: MasterResume, profile: Profile) -> str:
    parts = [p for p in (master.location, profile.contact.email, profile.contact.phone) if p]
    if profile.links.linkedin:
        parts.append(short_link(profile.links.linkedin))
    return " | ".join(parts)


def choose(group: Group, allow_disputed: bool) -> tuple[Variant | None, bool]:
    preferred = group.preferred()
    if preferred is not None:
        return preferred, False
    if allow_disputed:
        disputed = [v for v in group.variants if v.status == "disputed"]
        if disputed:
            return max(disputed, key=lambda v: len(v.sources)), True
    return None, False


def _bullet(variant: Variant, disputed: bool, split_label: bool) -> Bullet:
    match = _LABEL.match(variant.text) if split_label else None
    if match:
        return Bullet(text=match.group(2), label=match.group(1), disputed=disputed)
    return Bullet(text=variant.text, disputed=disputed)


def _bullets(groups: list[Group], allow_disputed: bool, split_label: bool) -> list[Bullet]:
    bullets = []
    for group in groups:
        if not group.default:
            continue
        variant, disputed = choose(group, allow_disputed)
        if variant is not None:
            bullets.append(_bullet(variant, disputed, split_label))
    return bullets


def base_document(master: MasterResume, profile: Profile) -> ResumeDoc:
    """The general resume: default groups, each with its most-used confirmed wording."""
    draft = master.meta.status != "confirmed"
    education = Section("Education")
    for edu in master.education:
        education.entries.append(
            Entry(
                heading_left=edu.school,
                heading_rest=f", {edu.note}" if edu.note else None,
                heading_right=f"{edu.location} | {edu.dates}",
                subtitle=edu.degree,
                bullets=_bullets(edu.groups, draft, split_label=True),
            )
        )
    experience = Section("Experience")
    for role in master.experience:
        if not role.default:
            continue
        experience.entries.append(
            Entry(
                heading_left=role.organization,
                heading_rest=f" ({role.descriptor})" if role.descriptor else None,
                heading_right=f"{role.location} | {role.dates}",
                subtitle=role.title,
                bullets=_bullets(role.groups, draft, split_label=False),
            )
        )
    sections = [education, experience]
    leadership = _bullets(master.leadership, draft, split_label=True)
    if leadership:
        sections.append(Section("Selected Projects and Leadership", [Entry(bullets=leadership)]))
    skills = _bullets(master.skills, draft, split_label=True)
    if skills:
        sections.append(Section("Skills", [Entry(bullets=skills)]))
    note = None
    if draft:
        note = "DRAFT: built from a master resume you have not confirmed yet."
        if any(b.disputed for s in sections for e in s.entries for b in e.bullets):
            note += " Highlighted lines wait on your answers."
    return ResumeDoc(name=master.name, contact_line=contact_line(master, profile), sections=sections, draft_note=note)


def render_html(doc: ResumeDoc) -> str:
    env = Environment(loader=FileSystemLoader(TEMPLATES), autoescape=select_autoescape(["html", "j2"]))
    return env.get_template("resume.html.j2").render(doc=doc)


def resume_filename(full_name: str, company: str | None = None) -> str:
    base = "_".join(full_name.split()) + "_Resume"
    if company:
        base += "_" + re.sub(r"[^A-Za-z0-9]+", "_", company).strip("_")
    return base + ".pdf"


def page_count(pdf: Path) -> int:
    return len(PdfReader(str(pdf)).pages)


def html_to_pdf(html: str, out: Path, browsers_dir: Path) -> None:
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(browsers_dir))
    from playwright.sync_api import sync_playwright

    out.parent.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        try:
            page = browser.new_page()
            page.set_content(html, wait_until="load")
            page.pdf(path=str(out), print_background=True, prefer_css_page_size=True)
        finally:
            browser.close()


@dataclass
class RenderResult:
    path: Path
    pages: int
    font_pt: float


def render_pdf(doc: ResumeDoc, out: Path, browsers_dir: Path, max_pages: int = 1) -> RenderResult:
    """Render, shrinking the font a little if needed; fails if it still does not fit."""
    problems = [f"{text[:60]!r}: {p}" for text in doc.all_text() for p in style_problems(text)]
    if problems:
        raise RenderError("Style rule broken (no contractions, no em dashes):\n  " + "\n  ".join(problems))
    for font_pt, margin_in in FIT_STEPS:
        doc.font_pt, doc.margin_in = font_pt, margin_in
        html_to_pdf(render_html(doc), out, browsers_dir)
        pages = page_count(out)
        if pages <= max_pages:
            return RenderResult(out, pages, font_pt)
    raise TooLong(f"The resume is {pages} pages even at {doc.font_pt}pt; it must be at most {max_pages}.")
