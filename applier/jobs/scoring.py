"""Score a posting 0 to 100 against config/targets.yaml, with a short factual reason for every point.

Points: role type in the title 40, level 20, years asked for 10, pay 10, industry 10, skills overlap 10.
A failed filter sets the decision (filtered_*) but the score is still recorded, for highlights.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from applier.config import Config, Targets
from applier.jobs.rules import (
    ai_policy_note,
    everify_note,
    hidden_instructions_note,
    location_verdict,
    not_full_time,
    required_years,
    sponsorship_stance,
)

# Words from your confirmed experience, used only to measure overlap with a posting.
SKILL_TERMS = [
    "excel", "powerpoint", "financial model", "forecast", "market sizing", "pricing", "go-to-market", "gtm",
    "segmentation", "stakeholder", "budget", "procurement", "vendor", "kpi", "dashboard", "program management",
    "project management", "survey", "experiment", "economics", "econometrics", "policy", "python", "stata",
    "gis", "arcgis", "ai", "llm", "automation", "operations", "strategy", "analytics", "research",
    "partnerships", "public sector", "government", "business case", "executive", "cross-functional",
]
_R_LANGUAGE = re.compile(r"\bR\b(?![&'])")
TECH_WORDS = ("tech", "technology", "software", "ai", "saas", "platform")


def _normalize(text: str) -> str:
    """'Strategy & Ops' and 'strategy and operations' read the same."""
    text = text.lower().replace("’", "'").replace("&", " and ")
    text = re.sub(r"\bops\b", "operations", text)
    text = re.sub(r"\bbiz\b", "business", text)
    text = re.sub(r"\bsr\b\.?", "senior", text)
    text = re.sub(r"\bjr\b\.?", "junior", text)
    return re.sub(r"\s+", " ", text)


def has_term(text: str, term: str) -> bool:
    pattern = r"(?<![a-z0-9])" + re.escape(_normalize(term).strip()) + r"(?![a-z0-9])"
    return re.search(pattern, _normalize(text)) is not None


@dataclass
class ScoreResult:
    score: int
    decision: str
    reasons: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    filters: list[str] = field(default_factory=list)
    required_years: int | None = None
    sponsorship_note: str | None = None
    ai_policy_note: str | None = None

    def explanation(self) -> str:
        lines = [f"Score {self.score}: " + "; ".join(self.reasons)]
        if self.filters:
            lines.append("Not eligible: " + "; ".join(self.filters))
        lines += self.notes
        return "\n".join(lines)


def _money(value: float) -> str:
    return f"${value:,.0f}"


def score_posting(posting: dict, company: dict, targets: Targets, config: Config) -> ScoreResult:
    title = posting.get("title") or ""
    text = posting.get("description_text") or ""
    everything = f"{title}\n{text}"
    points = 0
    reasons: list[str] = []
    filters: list[tuple[str, str]] = []  # (decision, reason)
    notes: list[str] = []

    # Location (brief 3): New York onsite or hybrid; remote US only if allowed.
    others = json.loads(posting.get("other_locations") or "[]")
    verdict = location_verdict(posting.get("location"), others, posting.get("workplace_type") or "unknown",
                               title, config.allow_remote_us)
    if not verdict.ok:
        filters.append(("filtered_location", verdict.reason))
    else:
        notes.append(f"Location: {verdict.reason}")

    # Role type (40): a role family named in the title.
    title_families = [name for name, terms in targets.role_families.items() if any(has_term(title, t) for t in terms)]
    for excluded in targets.exclude_title_keywords:
        if has_term(title, excluded):
            filters.append(("filtered_role", f"title contains '{excluded}'"))
            break
    if title_families:
        points += 40
        reasons.append(f"title matches {', '.join(f.replace('_', ' ') for f in title_families)} (+40)")
    else:
        body_families = [
            name for name, terms in targets.role_families.items()
            if any(len(t.split()) > 1 and has_term(text, t) for t in terms)
        ]
        if body_families:
            points += 15
            reasons.append(f"description mentions {', '.join(f.replace('_', ' ') for f in body_families)} (+15)")
        else:
            reasons.append("no target role type in title or description (+0)")

    # Level (20): below Manager preferred; Senior Manager and above excluded.
    level_reason = None
    for term in targets.seniority.excluded:
        if term == "chief" and has_term(title, "chief of staff"):
            continue
        if has_term(title, term):
            filters.append(("filtered_role", f"too senior: '{term}' in title"))
            level_reason = f"level '{term}' (+0)"
            break
    if level_reason is None:
        preferred = next((t for t in targets.seniority.preferred if has_term(title, t)), None)
        lower = next((t for t in targets.seniority.lower_score if has_term(title, t)), None)
        if preferred:
            points += 20
            level_reason = f"level '{preferred}' fits (+20)"
        elif lower:
            points += 8
            level_reason = f"level '{lower}' is above yours so far (+8)"
        else:
            points += 14
            level_reason = "no level in title (+14)"
    reasons.append(level_reason)

    # Years asked for (10).
    years = required_years(text)
    if years is None:
        points += 6
        reasons.append("years of experience not stated (+6)")
    else:
        earned = 10 if years <= 5 else 6 if years == 6 else 3 if years == 7 else 0
        points += earned
        reasons.append(f"asks for {years}+ years (+{earned})")

    # Pay (10) against your floor.
    floor = targets.salary_floor_usd
    low, high = posting.get("pay_min"), posting.get("pay_max")
    currency, interval = (posting.get("pay_currency") or "USD").upper(), posting.get("pay_interval") or "year"
    if low is None and high is None:
        points += 5
        reasons.append("no pay range posted (+5)")
    elif currency != "USD" or interval != "year":
        points += 3
        reasons.append(f"pay is not an annual USD range ({currency}, per {interval}) (+3)")
    else:
        low = low if low is not None else high
        high = high if high is not None else low
        mid = (low + high) / 2
        if high < floor:
            filters.append(("filtered_salary", f"top of range {_money(high)} is below your {_money(floor)} floor"))
            reasons.append(f"pay {_money(low)} to {_money(high)} (+0)")
        elif mid >= floor:
            points += 10
            reasons.append(f"pay {_money(low)} to {_money(high)}, midpoint at or above your floor (+10)")
        else:
            points += 5
            reasons.append(f"pay {_money(low)} to {_money(high)}, midpoint below your floor (+5)")

    # Startup filter (brief 3): private, founded after the cut-off, under the headcount limit.
    # A fact that is unknown or not confirmed by a source keeps the company in (and is flagged).
    facts = company.get("facts") or {}
    startup = config.startup_filter
    if not facts:
        notes.append("Startup facts not checked yet")
    else:
        unsure = set(facts.get("uncertain", []))
        year, people, private = facts.get("founded_year"), facts.get("headcount"), facts.get("is_private")
        if startup.private_only and private is False and "is_private" not in unsure:
            filters.append(("filtered_startup", "public company or owned by one"))
        if year and year <= startup.founded_after and "founded_year" not in unsure:
            filters.append(("filtered_startup", f"founded {year}, before {startup.founded_after + 1}"))
        if people and people >= startup.max_headcount and "headcount" not in unsure:
            filters.append(("filtered_startup", f"about {people:,} employees (limit {startup.max_headcount:,})"))
        described = ", ".join(filter(None, [
            f"founded {year}" if year else None,
            f"about {people:,} employees" if people else None,
            {True: "private", False: "public"}.get(private),
        ]))
        notes.append(f"Startup facts: {described or 'none confirmed'}"
                     + (f"; not confirmed: {', '.join(sorted(unsure))}" if unsure else ""))

    # Industry (10), from the company's recorded industry.
    industry = company.get("industry") or ""
    excluded = next((i for i in targets.industries.excluded if has_term(industry, i)), None) if industry else None
    if excluded in targets.industries.allowed_when_tech and any(has_term(industry, w) for w in TECH_WORDS):
        notes.append(f"Industry: '{industry}' is {excluded} tech, which you allow")
        excluded = None
    if excluded:
        filters.append(("filtered_industry", f"industry '{industry}' matches '{excluded}' on your excluded list"))
        reasons.append("excluded industry (+0)")
    elif industry and any(has_term(industry, i) for i in targets.industries.preferred):
        points += 10
        reasons.append(f"preferred industry: {industry} (+10)")
    elif industry and any(has_term(industry, i) for i in targets.industries.open):
        points += 5
        reasons.append(f"industry you are open to: {company.get('industry')} (+5)")
    else:
        points += 4
        reasons.append("industry not known yet (+4)" if not industry else f"industry {company.get('industry')} (+4)")

    # Skills overlap (10).
    hits = [t for t in SKILL_TERMS if has_term(everything, t)]
    if _R_LANGUAGE.search(text):
        hits.append("R")
    earned = min(10, len(hits))
    points += earned
    reasons.append(f"skills overlap: {', '.join(hits[:8]) or 'none'} (+{earned})")

    # Keyword rules from targets.yaml.
    if targets.must_have_keywords and not any(has_term(everything, k) for k in targets.must_have_keywords):
        filters.append(("filtered_role", "none of your must-have keywords appear"))
    for keyword in targets.exclude_description_keywords:
        if has_term(text, keyword):
            filters.append(("filtered_role", f"description mentions '{keyword}'"))
            break
    part_time = not_full_time(posting.get("employment_type"), title)
    if part_time:
        filters.append(("filtered_role", f"not a full-time role ({part_time})"))
    if (posting.get("employment_type") or "").lower().startswith("contract"):
        notes.append("Contract role")

    # Sponsorship (brief 4): you will need it in the future.
    stance, sentence = sponsorship_stance(text)
    if stance == "no" and targets.sponsorship.skip_if_company_cannot_sponsor:
        filters.append(("filtered_sponsorship", f'posting says: "{sentence}"'))
    elif stance == "yes":
        notes.append(f'Sponsorship: posting says "{sentence}"')
    if company.get("lca_note"):
        notes.append(company["lca_note"])

    # STEM OPT extension needs an E-Verify employer; postings sometimes say so.
    everify = everify_note(text) if targets.sponsorship.show_everify_status else None
    if everify:
        notes.append(f'E-Verify: posting says "{everify}"')

    # Rule 7: text aimed at AI tools is noted (and never followed).
    hidden = hidden_instructions_note(text)
    if hidden:
        notes.append(f'Possible instructions aimed at AI tools (ignored): "{hidden}"')

    # Rule 6: a request not to use AI blocks automatic submission later.
    ai_note = ai_policy_note(text)
    if ai_note:
        notes.append(f'Asks applicants not to use AI: "{ai_note}" (will not be auto-submitted)')

    decision = filters[0][0] if filters else "eligible"
    return ScoreResult(
        score=max(0, min(100, points)),
        decision=decision,
        reasons=reasons,
        notes=notes,
        filters=[reason for _, reason in filters],
        required_years=years,
        sponsorship_note=sentence,
        ai_policy_note=ai_note,
    )
