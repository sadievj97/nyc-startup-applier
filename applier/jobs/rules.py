"""Plain-text checks on a posting. Each returns the evidence it relied on, so every decision can be audited.

Job descriptions are untrusted data (rule 7): they are only searched with fixed patterns here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from applier.jobs.text import sentences

_NYC = re.compile(r"\b(new york|nyc|manhattan|brooklyn|queens|the bronx|long island city)\b", re.I)
_NY_ABBR = re.compile(r"\bNY\b")
_US = re.compile(r"\b(us|u\.s\.?|usa|united states|north america|americas|anywhere)\b", re.I)


def is_nyc(place: str | None) -> bool:
    return bool(place) and bool(_NYC.search(place) or _NY_ABBR.search(place))


@dataclass
class Verdict:
    ok: bool
    reason: str


def location_verdict(location: str | None, others: list[str], workplace: str, title: str,
                     allow_remote_us: bool) -> Verdict:
    places = [p for p in [location, *others] if p]
    nyc = any(is_nyc(p) for p in places)
    shown = location or (others[0] if others else "no location given")
    if workplace == "unknown":
        lowered = " ".join([*places, title]).lower()
        if "hybrid" in lowered:
            workplace = "hybrid"
        elif "remote" in lowered and not nyc:
            workplace = "remote"
    if workplace == "remote":
        if allow_remote_us and (nyc or any(_US.search(p) for p in places)):
            return Verdict(True, f"remote role in the US ({shown}); remote US is allowed in config")
        also = "; it also lists New York" if nyc else ""
        return Verdict(False, f"remote role ({shown}){also}; remote US roles are off in config")
    if nyc:
        kind = {"onsite": "onsite", "hybrid": "hybrid"}.get(workplace, "workplace type not stated")
        return Verdict(True, f"New York ({kind})")
    return Verdict(False, f"not in New York ({shown})")


# Statements that the company cannot sponsor a visa. You will need sponsorship in the future,
# so these postings are skipped (brief 4), with the sentence logged.
_NO_SPONSOR = [
    r"\b(unable|not able|cannot|can ?not|will not|won't|do(es)? not|don't|are not able) (to )?(provide |offer |support )?(visa |immigration |employment )?sponsor",
    r"\bno (visa |immigration )?sponsorship\b",
    r"\bsponsorship (is |will )?not (be )?(available|provided|offered)",
    r"\bwithout (the )?(need for )?(current or future |future |any )?(visa |employer |immigration )?sponsorship",
    r"\bnot (eligible|able) (for|to receive) (visa |immigration )?sponsorship",
    r"\b(u\.?s\.?|us) citizens?(hip)? (only|is required|required)",
    r"\bmust be a (u\.?s\.?|us) citizen",
    r"\bsecurity clearance\b",
]
_YES_SPONSOR = [
    r"\b(visa |h-?1b )?sponsorship (is )?available",
    r"\bwe (will |can |do )?sponsor (visas?|h-?1b|work authorization)",
    r"\bopen to sponsoring\b",
    r"\bh-?1b (sponsorship|transfers?) (is |are )?(available|supported|welcome)",
]


def sponsorship_stance(text: str) -> tuple[str | None, str | None]:
    """('no', sentence) if the posting says it cannot sponsor; ('yes', sentence) if it says it can."""
    for sentence in sentences(text):
        if any(re.search(p, sentence, re.I) for p in _NO_SPONSOR):
            return "no", sentence[:300]
    for sentence in sentences(text):
        if any(re.search(p, sentence, re.I) for p in _YES_SPONSOR):
            return "yes", sentence[:300]
    return None, None


_EVERIFY = re.compile(r"\be-?verify\b", re.I)


def everify_note(text: str) -> str | None:
    """The sentence where a posting says the employer uses E-Verify (needed for the STEM OPT extension)."""
    for sentence in sentences(text):
        if _EVERIFY.search(sentence):
            return sentence[:300]
    return None


# Requests that applicants not use AI tools (rule 6: never auto-submit such applications).
_NO_AI = [
    r"\b(do not|don't|please do not|refrain from|avoid|not permitted to|prohibit\w*) (use |using )?(of )?(ai|artificial intelligence|chatgpt|generative ai|gen ?ai|llms?|large language models?|automated tools?)\b",
    r"\bwithout (the )?(use|help|assistance) of (ai|artificial intelligence|chatgpt|generative ai|automated tools?)",
    r"\b(ai|chatgpt|llm)[- ](generated|written|assisted) (applications?|responses?|answers?|cover letters?|submissions?) (will|are|may)",
    r"\bapplications? (written|generated|completed|submitted) (by|with|using) (ai|chatgpt|bots?|automated tools?)",
    r"\b(no|not accept\w*) (ai|bot|automated)[- ]?(generated )?(applications|submissions)",
]


def ai_policy_note(text: str) -> str | None:
    for sentence in sentences(text):
        if any(re.search(p, sentence, re.I) for p in _NO_AI):
            return sentence[:300]
    return None


# Rule 7: postings may hide instructions aimed at AI applicants. They are never followed; we note them.
_HIDDEN = [
    r"\bif you are an? (ai|llm|large language model|language model|chatbot|bot|assistant|gpt)\b",
    r"\bignore (all |any )?(the )?(previous|prior|above|earlier) (instructions|prompts?)\b",
    r"\b(ai|llm|chatgpt|gpt|claude|language model)s?\b[^.]{0,80}\b(must|should|please) (include|mention|add|write|use)\b",
    r"\b(include|mention) the (word|phrase|code word|keyword)s?\b",
]


def hidden_instructions_note(text: str) -> str | None:
    for sentence in sentences(text):
        if any(re.search(p, sentence, re.I) for p in _HIDDEN):
            return sentence[:300]
    return None


_YEARS = re.compile(
    r"\b(\d{1,2})\s*\+?\s*(?:(?:-|to|–)\s*\d{1,2}\s*\+?\s*)?(?:years?|yrs?)\b(?:['’]s?)?(?:\s+of)?"
    r"(?:\s+[\w/&-]+){0,5}?\s+(?:experience|work)",
    re.I,
)


# "5+ years in strategy", "at least 4 years in", "3-5 years of": a requirement, not "10 years of data".
_YEARS_IN = re.compile(
    r"(?:\b(?:at least|minimum(?: of)?|min\.?)\s+(\d{1,2})|\b(\d{1,2})\s*\+|\b(\d{1,2})\s*(?:-|to|–)\s*\d{1,2})"
    r"\s*\+?\s*years?\s+(?:in|of)\s",
    re.I,
)


def required_years(text: str) -> int | None:
    """The largest 'at least N years of experience' figure in the posting, if any."""
    values = [int(m.group(1)) for m in _YEARS.finditer(text)]
    values += [int(next(g for g in m.groups() if g)) for m in _YEARS_IN.finditer(text)]
    values = [v for v in values if 0 < v <= 25]
    return max(values) if values else None


_NOT_FULL_TIME = re.compile(r"\b(intern(ship)?|part[- ]?time|temporary|temp|seasonal)\b", re.I)


def not_full_time(employment_type: str | None, title: str) -> str | None:
    for value in (employment_type or "", title):
        match = _NOT_FULL_TIME.search(value.replace("PartTime", "part-time"))
        if match:
            return match.group(0)
    return None
