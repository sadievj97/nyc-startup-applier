"""Company names: the normalized key used for the one-application-per-company rule, plus readers
for the company CSV files and the do-not-apply folders.

The key deliberately errs toward merging: two spellings of one company must never get two
applications, and wrongly merging two different companies only means one fewer application.
"""

from __future__ import annotations

import csv
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

# Legal-entity words dropped from the end of a name ("Acme Inc." == "Acme").
_LEGAL_SUFFIXES = {
    "inc", "incorporated", "llc", "ltd", "limited", "corp", "corporation", "co",
    "plc", "gmbh", "pbc", "lp", "llp", "sa", "ag", "bv",
}
# Generic tail words dropped from the end ("Acme AI" == "Acme", "Clay.com" == "Clay",
# "Bain & Company" == "Bain").
_GENERIC_TAILS = {"ai", "hq", "com", "io", "company"}
_DROPPABLE = _LEGAL_SUFFIXES | _GENERIC_TAILS
# Text in parentheses is a nickname or note: "Charles River Associates (CRA)".
_PARENTHESES = re.compile(r"\([^)]*\)")
# "AcmeAI" is written like "Acme AI".
_CAMEL_AI = re.compile(r"(?<=[a-z0-9])AI\b")
# Folders such as "0. Resume" or "1. Job Search Strategy" are not companies.
_NUMBERED_FOLDER = re.compile(r"^\d+\.\s")
# Never shorten a key below this many characters ("xAI" stays "xai", "Kai" stays "kai").
_MIN_KEY_LENGTH = 3


def company_key(name: str, aliases: dict[str, str] | None = None) -> str:
    """Normalized key for a company name. Aliases map one key to another (for typos)."""
    if not name or not name.strip():
        raise ValueError("company name is empty")
    text = _PARENTHESES.sub(" ", name).strip()
    if not re.search(r"[A-Za-z0-9]", text):
        text = name.strip()
    text = _CAMEL_AI.sub(" AI", text)
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    text = text.lower().replace("&", " and ").replace("'", "")
    tokens = re.findall(r"[a-z0-9]+", text)
    if len(tokens) > 1 and tokens[0] == "the":
        tokens = tokens[1:]

    def long_enough(remaining: list[str]) -> bool:
        return len("".join(remaining)) >= _MIN_KEY_LENGTH

    while len(tokens) > 1 and tokens[-1] in _DROPPABLE and long_enough(tokens[:-1]):
        dropped = tokens.pop()
        # "Bain & Company" -> "bain and" -> "bain"
        if dropped in {"company", "co"} and len(tokens) > 1 and tokens[-1] == "and" and long_enough(tokens[:-1]):
            tokens.pop()
    key = "".join(tokens)
    if not key:
        raise ValueError(f"company name {name!r} has no letters or digits")
    if aliases:
        key = aliases.get(key, key)
    return key


# ---------------------------------------------------------------- CSV files


def _read_csv(path: Path, required: Iterable[str]) -> list[dict[str, str]]:
    """Read a CSV saved by Excel or Notepad. Blank rows are skipped; headers are case-insensitive."""
    if not path.exists():
        return []
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        headers = [h.strip().lower() for h in (reader.fieldnames or [])]
        missing = [col for col in required if col not in headers]
        if missing:
            raise ValueError(f"{path.name} is missing column(s): {', '.join(missing)}")
        rows = []
        for raw in reader:
            row = {(k or "").strip().lower(): (v or "").strip() for k, v in raw.items()}
            if any(row.values()):
                rows.append(row)
        return rows


def load_aliases(path: Path) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for row in _read_csv(path, ["alias", "canonical"]):
        if row["alias"] and row["canonical"]:
            aliases[company_key(row["alias"])] = company_key(row["canonical"])
    return aliases


def _yes(value: str, default: bool) -> bool:
    if not value:
        return default
    return value.strip().lower() in {"yes", "y", "true", "1"}


@dataclass(frozen=True)
class ListedCompany:
    name: str
    website: str
    highlight: bool
    source: str


def read_applied_before(path: Path) -> list[ListedCompany]:
    # Companies applied to before the agent existed are always watched for highlights (brief 4b).
    return [
        ListedCompany(row["company"], row.get("website", ""), True, path.name)
        for row in _read_csv(path, ["company"])
        if row["company"]
    ]


def read_do_not_apply(path: Path) -> list[ListedCompany]:
    return [
        ListedCompany(row["company"], row.get("website", ""), _yes(row.get("highlight", ""), False), path.name)
        for row in _read_csv(path, ["company"])
        if row["company"]
    ]


# ---------------------------------------------------------------- do-not-apply folders


def folder_company_name(folder_name: str) -> str | None:
    """'Edra - Startup' -> 'Edra'; '0. Resume' -> None."""
    name = folder_name.strip()
    if not name or _NUMBERED_FOLDER.match(name):
        return None
    name = name.split(" - ")[0].strip()
    return name or None


def read_company_folders(folder: Path, ignore: Iterable[str], highlight: bool) -> list[ListedCompany]:
    """Each subfolder of `folder` is a company. Raises FileNotFoundError if the folder is unreachable."""
    if not folder.is_dir():
        raise FileNotFoundError(f"do-not-apply folder not found: {folder}")
    ignored = {item.strip().lower() for item in ignore}
    companies = []
    for child in sorted(folder.iterdir(), key=lambda p: p.name.lower()):
        if not child.is_dir() or child.name.strip().lower() in ignored:
            continue
        name = folder_company_name(child.name)
        if name:
            companies.append(ListedCompany(name, "", highlight, f"folder: {child}"))
    return companies
