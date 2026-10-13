"""Hacker News 'Ask HN: Who is hiring?' discovery through the Algolia HN Search API (brief 3).

Posts are untrusted text (rule 7): only fixed patterns read them, to get a company name and links.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field

from applier.jobs.detect import NOT_A_COMPANY_SITE, BoardRef, boards_in_text, is_blocked_site
from applier.jobs.http import PoliteClient
from applier.jobs.rules import is_nyc
from applier.jobs.text import html_to_text

ALGOLIA = "https://hn.algolia.com/api/v1"
_TITLE = re.compile(r"^Ask HN: Who is hiring\? \((\w+ \d{4})\)", re.I)
_HREF = re.compile(r'href="([^"]+)"')
_NYC_ANYWHERE = re.compile(r"\b(new york|nyc|manhattan|brooklyn)\b", re.I)


@dataclass
class HNThread:
    id: int
    title: str
    month: str


@dataclass
class HNPost:
    id: int
    company: str
    header: str
    urls: list[str] = field(default_factory=list)
    boards: list[BoardRef] = field(default_factory=list)

    @property
    def website(self) -> str | None:
        for url in self.urls:
            if not boards_in_text(url) and not is_blocked_site(url, NOT_A_COMPANY_SITE):
                return url
        return None


def latest_thread(client: PoliteClient, month: str | None = None) -> HNThread | None:
    data = client.get_json(f"{ALGOLIA}/search_by_date",
                           params={"tags": "story,author_whoishiring", "hitsPerPage": 30})
    for hit in (data or {}).get("hits", []):
        match = _TITLE.match(hit.get("title") or "")
        if match and (month is None or match.group(1).lower() == month.lower()):
            return HNThread(int(hit["objectID"]), hit["title"], match.group(1))
    return None


def _company_from_header(header: str) -> str | None:
    first = re.split(r"\s+[|•–-]\s+|\s*\|\s*", header, maxsplit=1)[0].strip()
    first = re.sub(r"\s*\([^)]*\)", "", first).strip()  # "Acme (YC W21)", "Acme (Series B)" -> "Acme"
    first = re.sub(r"^https?://", "", first).split("/")[0] if first.startswith("http") else first
    if not first or len(first) > 60 or len(first.split()) > 6:
        return None
    return first


def parse_post(item: dict) -> HNPost | None:
    raw = item.get("text") or ""
    if not raw:
        return None
    text = html_to_text(raw)
    header = text.splitlines()[0] if text else ""
    if not (is_nyc(header) or _NYC_ANYWHERE.search(text)):
        return None
    company = _company_from_header(header)
    if company is None:
        return None
    urls = [html.unescape(u) for u in _HREF.findall(raw)]
    return HNPost(id=int(item["id"]), company=company, header=header[:300], urls=urls,
                  boards=boards_in_text(" ".join(urls) + " " + text))


def nyc_posts(client: PoliteClient, thread: HNThread) -> list[HNPost]:
    data = client.get_json(f"{ALGOLIA}/items/{thread.id}")
    posts = []
    for child in (data or {}).get("children", []):
        post = parse_post(child)
        if post:
            posts.append(post)
    return posts
