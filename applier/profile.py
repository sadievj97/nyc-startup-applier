"""Load profile/profile.yaml: the only source for answers to questions the user owns (rule 2).

Also holds the salary rule, because salary answers must come from profile values by a fixed
calculation, never from a model's judgment.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Literal

from pydantic import Field, ValidationError, model_validator

from applier.config import ConfigError, StrictModel, _explain, _read_yaml


class Name(StrictModel):
    first: str
    last: str
    full: str
    preferred_first: str | None = None


class Contact(StrictModel):
    email: str = Field(pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    phone: str
    # The Google account whose mailbox holds the email above, if it is not the same address (Gmail sign-in only).
    inbox_account: str | None = Field(None, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class Location(StrictModel):
    city: str
    state: str
    country: str
    display: str


class Links(StrictModel):
    linkedin: str | None = None
    website: str | None = None
    portfolio: str | None = None
    github: str | None = None


class WorkAuthorization(StrictModel):
    authorized_to_work_in_us: bool
    status: str
    description: str
    opt_end_date: date | None = None
    plans_stem_opt_extension: bool = False


class Sponsorship(StrictModel):
    requires_now: bool
    requires_in_future: bool
    requires_now_or_in_future: bool

    @model_validator(mode="after")
    def _consistent(self) -> "Sponsorship":
        if self.requires_now_or_in_future != (self.requires_now or self.requires_in_future):
            raise ValueError("requires_now_or_in_future must be true if either of the other two is true")
        return self


class StartDate(StrictModel):
    answer: str
    notice_weeks: int | None = None


class Salary(StrictModel):
    rule: Literal["midpoint_of_posted_range"] = "midpoint_of_posted_range"
    expected_min_usd: int = Field(ge=0)
    expected_max_usd: int = Field(ge=0)
    floor_usd: int = Field(ge=0)

    @model_validator(mode="after")
    def _ordered(self) -> "Salary":
        if self.expected_min_usd > self.expected_max_usd:
            raise ValueError("expected_min_usd must not be larger than expected_max_usd")
        return self


class Relocation(StrictModel):
    willing: bool
    open_to: list[str] = []


class FormAnswers(StrictModel):
    onsite_new_york_5_days: bool | None = None
    future_contact_consent: bool | None = None


class Eeo(StrictModel):
    gender: str
    race_ethnicity: str
    hispanic_latino: str
    veteran_status: str
    disability_status: str
    sexual_orientation: str
    transgender: str
    pronouns: str


class Profile(StrictModel):
    name: Name
    contact: Contact
    location: Location
    links: Links = Links()
    over_18: bool
    work_authorization: WorkAuthorization
    sponsorship: Sponsorship
    start_date: StartDate
    salary: Salary
    relocation: Relocation
    how_did_you_hear: str
    form_answers: FormAnswers = FormAnswers()
    eeo: Eeo

    @property
    def needs_sponsorship(self) -> bool:
        return self.sponsorship.requires_now_or_in_future


def load_profile(path: Path) -> Profile:
    try:
        return Profile.model_validate(_read_yaml(path))
    except ValidationError as exc:
        raise _explain(path, exc) from None


# ---------------------------------------------------------------- salary rule


@dataclass(frozen=True)
class SalaryAnswer:
    """What to put in a salary field. If `needs_me` is set, do not answer: flag the application."""

    number: int | None
    text: str | None
    basis: str
    needs_me: str | None = None


def _usd(amount: int) -> str:
    return f"${amount:,}"


def salary_answer(
    salary: Salary,
    posted_min: float | None,
    posted_max: float | None,
    currency: str | None = "USD",
    interval: str | None = "year",
) -> SalaryAnswer:
    """Apply the user's rule: midpoint of the posted range, else their expected range.

    Anything the rule does not cover (hourly pay, other currencies, open-ended ranges, a
    midpoint below the floor) returns needs_me instead of a guess.
    """
    if posted_min is None and posted_max is None:
        mid = (salary.expected_min_usd + salary.expected_max_usd) // 2
        return SalaryAnswer(
            number=mid,
            text=f"{_usd(salary.expected_min_usd)} to {_usd(salary.expected_max_usd)}",
            basis="no posted range: your expected range from profile.yaml",
        )

    if (currency or "USD").upper() != "USD":
        return SalaryAnswer(None, None, "posted range", needs_me=f"posted pay is in {currency}, not USD")
    if (interval or "year").lower() not in {"year", "yearly", "annual", "annually", "per year", "1 year"}:
        return SalaryAnswer(None, None, "posted range", needs_me=f"posted pay is per {interval}, not per year")
    if posted_min is None or posted_max is None:
        return SalaryAnswer(None, None, "posted range", needs_me="posted range has only one end")
    if posted_min > posted_max:
        posted_min, posted_max = posted_max, posted_min

    mid = round((posted_min + posted_max) / 2)
    if mid < salary.floor_usd:
        return SalaryAnswer(
            None,
            None,
            "posted range",
            needs_me=f"midpoint {_usd(mid)} is below your {_usd(salary.floor_usd)} floor",
        )
    return SalaryAnswer(
        number=mid,
        text=_usd(mid),
        basis=f"midpoint of posted range {_usd(round(posted_min))} to {_usd(round(posted_max))}",
    )
