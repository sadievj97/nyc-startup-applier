"""Work out which hiring system a company uses, and its board name (brief 3).

First look for job-board links in text (a careers page, an HN post); if none, try the public APIs
with a few likely board names. Boards on other systems are recorded as manual.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlparse

from applier.jobs.ats import fetch_board
from applier.jobs.http import FetchError, PoliteClient

_TOKEN = r"([A-Za-z0-9][A-Za-z0-9._-]*)"
BOARD_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("ashby", re.compile(r"jobs\.ashbyhq\.com/" + _TOKEN)),
    ("ashby", re.compile(r"api\.ashbyhq\.com/posting-api/job-board/" + _TOKEN)),
    ("greenhouse", re.compile(r"greenhouse\.io/embed/job_board(?:/js)?\?for=" + _TOKEN)),
    ("greenhouse", re.compile(r"boards-api\.greenhouse\.io/v1/boards/" + _TOKEN)),
    ("greenhouse", re.compile(r"(?:job-boards|boards)(?:\.eu)?\.greenhouse\.io/(?!embed\b)" + _TOKEN)),
    ("lever", re.compile(r"jobs\.(?:eu\.)?lever\.co/" + _TOKEN)),
    ("workable", re.compile(r"apply\.workable\.com/(?!api\b)" + _TOKEN)),
]
# Systems with no public API this agent may use: tracked, marked manual.
MANUAL_MARKERS = {
    "myworkdayjobs.com": "Workday",
    "icims.com": "iCIMS",
    "smartrecruiters.com": "SmartRecruiters",
    "bamboohr.com": "BambooHR",
    "jazzhr.com": "JazzHR",
    "recruitee.com": "Recruitee",
    "rippling.com/careers": "Rippling",
    "dover.com": "Dover",
    "workable.com": "Workable",
    "teamtailor.com": "Teamtailor",
    "breezy.hr": "Breezy",
}
_NOT_TOKENS = {"embed", "api", "jobs", "www", "careers"}

# Rule 4: job boards and networks are never opened by the agent. Other sites here are not company
# websites, so they are never used to guess a board name either.
NEVER_FETCH = (
    "linkedin.com", "indeed.com", "wellfound.com", "angel.co", "builtin.com", "builtinnyc.com",
    "glassdoor.com", "ziprecruiter.com", "workatastartup.com", "otta.com", "welcometothejungle.com",
    "dice.com", "hired.com", "monster.com", "simplyhired.com", "jobs.lever.co.uk",
)
NOT_A_COMPANY_SITE = NEVER_FETCH + (
    "ycombinator.com", "github.com", "twitter.com", "x.com", "forms.gle", "docs.google.com",
    "calendly.com", "notion.so", "notion.site", "medium.com", "substack.com", "youtube.com", "loom.com",
    "typeform.com", "airtable.com", "tally.so",
)


def _host(url: str) -> str:
    return urlparse(url if "//" in url else "//" + url).netloc.lower().removeprefix("www.")


def is_blocked_site(url: str, domains: tuple[str, ...] = NEVER_FETCH) -> bool:
    host = _host(url)
    return any(host == d or host.endswith("." + d) for d in domains)


@dataclass(frozen=True)
class BoardRef:
    ats: str  # ashby | greenhouse | lever | workable | manual
    token: str | None
    how: str  # how it was found, for the record


def boards_in_text(text: str) -> list[BoardRef]:
    found: list[BoardRef] = []
    for ats, pattern in BOARD_PATTERNS:
        for match in pattern.finditer(text):
            token = match.group(1).rstrip(".")
            if token.lower() in _NOT_TOKENS:
                continue
            ref = BoardRef(ats, token, "link on page")
            if all((r.ats, r.token.lower()) != (ats, token.lower()) for r in found):
                found.append(ref)
    return found


def manual_system_in_text(text: str) -> str | None:
    lowered = text.lower()
    for marker, name in MANUAL_MARKERS.items():
        if marker in lowered:
            return name
    return None


def slug_candidates(name: str, website: str | None = None) -> list[str]:
    words = re.findall(r"[a-z0-9]+", name.lower())
    words = [w for w in words if w not in {"inc", "llc", "co", "corp", "the", "hq"}]
    candidates = []
    if words:
        candidates += ["".join(words), "-".join(words), words[0]]
    if website and not is_blocked_site(website, NOT_A_COMPANY_SITE):
        host = _host(website)
        if host:
            candidates.append(host.split(".")[0])
    unique = []
    for c in candidates:
        if c and len(c) >= 2 and c not in unique:
            unique.append(c)
    return unique[:4]


def _mentions_company(postings, name: str) -> bool:
    """A guessed board counts only if its postings name the company (so 'linkedin' is not Koop AI)."""
    words = [w for w in re.findall(r"[a-z0-9]+", name.lower()) if len(w) >= 3 and w not in {"inc", "labs", "the"}]
    needles = {"".join(words), *(words[:1])} - {""}
    for posting in postings[:15]:
        text = f"{posting.title} {posting.description_text}".lower()
        squashed = re.sub(r"[^a-z0-9]", "", text)
        if any(n in text or n in squashed for n in needles):
            return True
    return False


def probe_boards(client: PoliteClient, name: str, candidates: list[str]) -> BoardRef | None:
    """Try likely board names on each public API. A board counts only if it has live postings that
    mention the company."""
    for ats in ("ashby", "greenhouse", "lever"):
        for slug in candidates:
            try:
                postings = fetch_board(client, ats, slug)
            except FetchError:
                continue
            if postings and _mentions_company(postings, name):
                return BoardRef(ats, slug, f"{ats} board '{slug}' found by name; its postings mention {name}")
    return None


def detect(client: PoliteClient, name: str, website: str | None, careers_url: str | None,
           hint_text: str = "") -> BoardRef | None:
    refs = boards_in_text(hint_text)
    if refs:
        return refs[0]
    for url in filter(None, (careers_url, website)):
        if not url.startswith("http"):
            url = "https://" + url
        if is_blocked_site(url, NOT_A_COMPANY_SITE):
            continue  # rule 4: never open job boards or networks
        try:
            page = client.get_text(url)
        except FetchError:
            continue
        if not page:
            continue
        refs = boards_in_text(page)
        if refs:
            return BoardRef(refs[0].ats, refs[0].token, f"link on {url}")
        manual = manual_system_in_text(page)
        if manual:
            return BoardRef("manual", None, f"{manual} board on {url}")
    return probe_boards(client, name, slug_candidates(name, website))
