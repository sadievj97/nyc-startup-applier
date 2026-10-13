"""Read the user's resume files (profile/source/*.docx or *.pdf) into paragraphs.

Word files are preferred: their text is exact. PDF text extraction can split words, so PDFs are
only used when no Word version exists.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

Kind = Literal["name", "contact", "section", "entry", "bullet", "text"]

_BULLET_CHARS = "•▪●-"
_SECTION = re.compile(r"^[A-Z][A-Z0-9 &,/]+$")
# Section headings some resumes write in normal capitalization.
_KNOWN_SECTIONS = {
    "summary", "education", "experience", "professional experience", "relevant experience",
    "leadership", "skills", "skills and additional", "skills and interests", "additional",
}


def _is_section(text: str) -> bool:
    return (bool(_SECTION.match(text)) and len(text) > 3) or text.strip().lower() in _KNOWN_SECTIONS
_DATES = re.compile(r"(\b(19|20)\d{2}\b|\bPresent\b)")
_QUOTES = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"', " ": " "})
SOURCE_SUFFIXES = (".docx", ".pdf")


def normalize(text: str) -> str:
    """Comparison form: straight quotes, en dashes as hyphens, single spaces."""
    text = text.translate(_QUOTES).replace("–", "-")
    return re.sub(r"\s+", " ", text).strip()


def paragraph_id(text: str) -> str:
    return "p" + hashlib.sha1(normalize(text).encode("utf-8")).hexdigest()[:7]


@dataclass
class Paragraph:
    text: str
    kind: Kind
    section: str = ""
    context: str = ""  # the entry heading(s) this paragraph sits under

    @property
    def id(self) -> str:
        return paragraph_id(self.text)


@dataclass
class SourceDoc:
    path: Path
    paragraphs: list[Paragraph] = field(default_factory=list)

    @property
    def name(self) -> str:
        return self.path.name

    def normalized_text(self) -> str:
        return normalize(" ".join(p.text for p in self.paragraphs))


def _raw_docx(path: Path) -> list[tuple[str, bool]]:
    import docx  # python-docx

    lines = []
    for para in docx.Document(str(path)).paragraphs:
        text = para.text.strip()
        if not text:
            continue
        ppr = para._p.pPr
        numbered = ppr is not None and ppr.numPr is not None
        style = (para.style.name if para.style is not None else "") or ""
        lines.append((text, numbered or style.lower().startswith("list")))
    return lines


def _raw_pdf(path: Path) -> list[tuple[str, bool]]:
    from pypdf import PdfReader

    lines: list[tuple[str, bool]] = []
    for page in PdfReader(str(path)).pages:
        for raw in (page.extract_text() or "").splitlines():
            text = raw.strip()
            if not text:
                continue
            if text[0] in _BULLET_CHARS:
                lines.append((text.lstrip(_BULLET_CHARS).strip(), True))
            elif lines and lines[-1][1] and not _is_section(text) and not _DATES.search(text):
                lines[-1] = (lines[-1][0] + " " + text, True)  # a bullet that wrapped onto the next line
            else:
                lines.append((text, False))
    return lines


def read_source(path: Path) -> SourceDoc:
    raw = _raw_docx(path) if path.suffix.lower() == ".docx" else _raw_pdf(path)
    doc = SourceDoc(path)
    section, heading, previous_bullet = "", [], False
    for index, (text, is_bullet) in enumerate(raw):
        kind: Kind
        if index == 0:
            kind = "name"
        elif index == 1 and ("@" in text or "|" in text):
            kind = "contact"
        elif is_bullet:
            kind = "bullet"
        elif _is_section(text):
            kind = "section"
        elif "SKILL" in section.upper():
            kind = "text"
        else:
            kind = "entry"

        if kind == "section":
            section, heading = text, []
        elif kind == "entry":
            if previous_bullet:
                heading = []
            heading.append(text.split("\t")[0].strip())
        previous_bullet = kind == "bullet"
        doc.paragraphs.append(Paragraph(text, kind, section, " / ".join(heading)))
    return doc


def list_sources(folder: Path) -> list[Path]:
    if not folder.is_dir():
        return []
    return sorted(p for p in folder.iterdir() if p.suffix.lower() in SOURCE_SUFFIXES and not p.name.startswith("~$"))


def read_sources(folder: Path) -> list[SourceDoc]:
    return [read_source(path) for path in list_sources(folder)]
