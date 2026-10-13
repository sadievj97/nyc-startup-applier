"""Public job-board APIs for Ashby, Greenhouse and Lever (endpoints checked against their docs, 2026-10-12).

Only public posting endpoints are read. The application-submission APIs are never used (brief 6).
Parsing is separate from fetching so it can be tested with saved responses.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from applier.jobs.http import PoliteClient
from applier.jobs.text import html_to_text

ASHBY_BOARD = "https://api.ashbyhq.com/posting-api/job-board/{board}"
GREENHOUSE_JOBS = "https://boards-api.greenhouse.io/v1/boards/{token}/jobs"
GREENHOUSE_JOB = "https://boards-api.greenhouse.io/v1/boards/{token}/jobs/{job_id}"
LEVER_POSTINGS = "https://api.lever.co/v0/postings/{site}"
LEVER_POSTINGS_EU = "https://api.eu.lever.co/v0/postings/{site}"

FETCHABLE = ("ashby", "greenhouse", "lever")


@dataclass
class Posting:
    ats: str
    ats_posting_id: str
    title: str
    location: str | None = None
    other_locations: list[str] = field(default_factory=list)
    workplace_type: str = "unknown"  # onsite | hybrid | remote | unknown
    employment_type: str | None = None
    department: str | None = None
    team: str | None = None
    is_listed: bool = True
    url: str | None = None
    apply_url: str | None = None
    pay_min: float | None = None
    pay_max: float | None = None
    pay_currency: str | None = None
    pay_interval: str | None = None  # year | month | hour | other
    pay_raw: str | None = None
    published_at: str | None = None
    source_updated_at: str | None = None
    description_raw: str | None = None
    description_text: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


def _interval(raw: str | None) -> str | None:
    if not raw:
        return None
    text = raw.lower()
    for word in ("year", "month", "week", "day", "hour"):
        if word in text:
            return word
    return "other"


def _iso_from_ms(ms: Any) -> str | None:
    try:
        return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- Ashby

_ASHBY_WORKPLACE = {"onsite": "onsite", "remote": "remote", "hybrid": "hybrid"}


def parse_ashby(data: dict, board: str) -> list[Posting]:
    postings = []
    for job in data.get("jobs") or []:
        if job.get("isListed") is False:
            continue  # brief: skip unlisted Ashby postings
        job_url = job.get("jobUrl") or ""
        posting_id = job.get("id") or job_url.rstrip("/").rsplit("/", 1)[-1]
        if not posting_id:
            continue
        workplace = _ASHBY_WORKPLACE.get(str(job.get("workplaceType", "")).lower().replace("-", ""), "unknown")
        if workplace == "unknown" and job.get("isRemote"):
            workplace = "remote"
        others = [s.get("location") for s in job.get("secondaryLocations") or [] if s.get("location")]
        postal = (job.get("address") or {}).get("postalAddress") or {}
        place = ", ".join(p for p in (postal.get("addressLocality"), postal.get("addressRegion")) if p)
        if place:
            others.append(place)
        posting = Posting(
            ats="ashby",
            ats_posting_id=str(posting_id),
            title=(job.get("title") or "").strip(),
            location=job.get("location"),
            other_locations=others,
            workplace_type=workplace,
            employment_type=job.get("employmentType"),
            department=job.get("department"),
            team=job.get("team"),
            url=job_url or None,
            apply_url=job.get("applyUrl"),
            published_at=job.get("publishedAt"),
            description_raw=job.get("descriptionHtml"),
            description_text=job.get("descriptionPlain") or html_to_text(job.get("descriptionHtml")),
            extra={"board": board},
        )
        comp = job.get("compensation") or {}
        posting.pay_raw = comp.get("compensationTierSummary") or comp.get("scrapeableCompensationSalarySummary")
        for component in comp.get("summaryComponents") or []:
            if str(component.get("compensationType", "")).lower() == "salary":
                posting.pay_min = component.get("minValue")
                posting.pay_max = component.get("maxValue")
                posting.pay_currency = component.get("currencyCode")
                posting.pay_interval = _interval(component.get("interval"))
                break
        postings.append(posting)
    return postings


def fetch_ashby(client: PoliteClient, board: str) -> list[Posting] | None:
    data = client.get_json(ASHBY_BOARD.format(board=board), params={"includeCompensation": "true"})
    return None if data is None else parse_ashby(data, board)


# ---------------------------------------------------------------- Greenhouse


def _greenhouse_workplace(job: dict) -> str:
    for item in job.get("metadata") or []:
        name = str(item.get("name", "")).lower()
        value = str(item.get("value", "")).lower()
        if "remote" in name or "workplace" in name or "location type" in name:
            for key in ("hybrid", "remote", "onsite", "on-site", "in office", "in-office"):
                if key in value:
                    return {"on-site": "onsite", "in office": "onsite", "in-office": "onsite"}.get(key, key)
    location = str((job.get("location") or {}).get("name", "")).lower()
    if "hybrid" in location:
        return "hybrid"
    if "remote" in location:
        return "remote"
    return "unknown"


def parse_greenhouse(data: dict, token: str) -> list[Posting]:
    postings = []
    for job in data.get("jobs") or []:
        content = job.get("content")
        others = [o.get("location") or o.get("name") for o in job.get("offices") or []]
        postings.append(
            Posting(
                ats="greenhouse",
                ats_posting_id=str(job.get("id")),
                title=(job.get("title") or "").strip(),
                location=(job.get("location") or {}).get("name"),
                other_locations=[o for o in others if o],
                workplace_type=_greenhouse_workplace(job),
                department=", ".join(d.get("name", "") for d in job.get("departments") or []) or None,
                url=job.get("absolute_url"),
                apply_url=job.get("absolute_url"),
                published_at=job.get("first_published"),
                source_updated_at=job.get("updated_at"),
                description_raw=content,
                description_text=html_to_text(content),
                extra={"board": token},
            )
        )
    return postings


def apply_greenhouse_details(posting: Posting, detail: dict) -> None:
    """Add pay range, AI disclaimer flag and form questions from the single-job endpoint."""
    if detail.get("first_published"):
        posting.published_at = detail["first_published"]
    if "include_ai_disclaimer" in detail:
        posting.extra["include_ai_disclaimer"] = bool(detail.get("include_ai_disclaimer"))
    if detail.get("questions"):
        posting.extra["questions"] = detail["questions"]
    for pay in detail.get("pay_input_ranges") or []:
        if pay.get("min_cents") is None and pay.get("max_cents") is None:
            continue
        posting.pay_min = pay["min_cents"] / 100 if pay.get("min_cents") is not None else None
        posting.pay_max = pay["max_cents"] / 100 if pay.get("max_cents") is not None else None
        posting.pay_currency = pay.get("currency_type")
        posting.pay_interval = "year"  # Greenhouse pay ranges are annual unless the blurb says otherwise
        posting.pay_raw = " ".join(filter(None, (pay.get("title"), html_to_text(pay.get("blurb")))))[:300] or None
        if "hour" in (posting.pay_raw or "").lower():
            posting.pay_interval = "hour"
        if pay.get("title") and "new york" in pay["title"].lower():
            break  # prefer the New York range when several are listed


def fetch_greenhouse(client: PoliteClient, token: str) -> list[Posting] | None:
    data = client.get_json(GREENHOUSE_JOBS.format(token=token), params={"content": "true"})
    return None if data is None else parse_greenhouse(data, token)


def fetch_greenhouse_details(client: PoliteClient, token: str, posting: Posting) -> None:
    detail = client.get_json(
        GREENHOUSE_JOB.format(token=token, job_id=posting.ats_posting_id),
        params={"questions": "true", "pay_transparency": "true"},
    )
    if detail:
        apply_greenhouse_details(posting, detail)


# ---------------------------------------------------------------- Lever

_LEVER_WORKPLACE = {"on-site": "onsite", "onsite": "onsite", "remote": "remote", "hybrid": "hybrid"}


def parse_lever(data: list, site: str) -> list[Posting]:
    postings = []
    for item in data or []:
        categories = item.get("categories") or {}
        lists = item.get("lists") or []
        parts_html = [item.get("description") or ""]
        parts_text = [item.get("descriptionPlain") or html_to_text(item.get("description"))]
        for block in lists:
            parts_html.append(f"<h3>{block.get('text', '')}</h3><ul>{block.get('content', '')}</ul>")
            parts_text.append(block.get("text", "") + "\n" + html_to_text(f"<ul>{block.get('content', '')}</ul>"))
        parts_html.append(item.get("additional") or "")
        parts_text.append(item.get("additionalPlain") or html_to_text(item.get("additional")))
        salary = item.get("salaryRange") or {}
        others = [loc for loc in categories.get("allLocations") or [] if loc and loc != categories.get("location")]
        postings.append(
            Posting(
                ats="lever",
                ats_posting_id=str(item.get("id")),
                title=(item.get("text") or "").strip(),
                location=categories.get("location"),
                other_locations=others,
                workplace_type=_LEVER_WORKPLACE.get(str(item.get("workplaceType", "")).lower(), "unknown"),
                employment_type=categories.get("commitment"),
                department=categories.get("department"),
                team=categories.get("team"),
                url=item.get("hostedUrl"),
                apply_url=item.get("applyUrl"),
                pay_min=salary.get("min"),
                pay_max=salary.get("max"),
                pay_currency=salary.get("currency"),
                pay_interval=_interval(salary.get("interval")),
                pay_raw=item.get("salaryDescriptionPlain"),
                published_at=_iso_from_ms(item.get("createdAt")),
                description_raw="\n".join(parts_html),
                description_text="\n\n".join(p for p in parts_text if p).strip(),
                extra={"board": site, "country": item.get("country")},
            )
        )
    return postings


def fetch_lever(client: PoliteClient, site: str) -> list[Posting] | None:
    data = client.get_json(LEVER_POSTINGS.format(site=site), params={"mode": "json"})
    if data is None:
        data = client.get_json(LEVER_POSTINGS_EU.format(site=site), params={"mode": "json"})
    return None if data is None else parse_lever(data, site)


def fetch_board(client: PoliteClient, ats: str, token: str) -> list[Posting] | None:
    """All postings on a board, or None if the board does not exist."""
    if ats == "ashby":
        return fetch_ashby(client, token)
    if ats == "greenhouse":
        return fetch_greenhouse(client, token)
    if ats == "lever":
        return fetch_lever(client, token)
    raise ValueError(f"cannot fetch {ats} boards")
