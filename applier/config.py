"""Load and validate config/config.yaml and config/targets.yaml.

Unknown keys are rejected so a typo in a setting is caught instead of silently ignored.
"""

from __future__ import annotations

from datetime import date, time
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator


class ConfigError(Exception):
    """A settings file is missing or has an invalid value. The message is written for a non-programmer."""


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SubmitWindow(StrictModel):
    start: time = time(8, 0)
    end: time = time(20, 0)
    timezone: str = "America/New_York"

    @field_validator("timezone")
    @classmethod
    def _known_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown time zone {value!r}") from exc
        return value

    @model_validator(mode="after")
    def _start_before_end(self) -> "SubmitWindow":
        if self.start >= self.end:
            raise ValueError("submit_window start must be earlier than end")
        return self

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


class StartupFilter(StrictModel):
    private_only: bool = True
    founded_after: int = Field(2014, ge=1900, le=2100)
    max_headcount: int = Field(500, ge=1)
    keep_when_uncertain: bool = True


class Models(StrictModel):
    tailoring: str = "claude-sonnet-5-5"
    form_mapping: str = "claude-sonnet-5-5"
    fact_check: str = "claude-opus-5-5"
    research: str = "claude-opus-5-5"
    email_classification: str = "claude-haiku-5-5"
    networking: str = "claude-sonnet-5-5"


class Schedule(StrictModel):
    digest: time = time(7, 30)
    daily_run: time = time(8, 15)


class Config(StrictModel):
    enabled: bool = True
    mode: Literal["dry_run", "review", "auto"] = "dry_run"
    review_first_n: int = Field(10, ge=0)
    max_applications_per_day: int = Field(5, ge=0, le=50)
    submit_window: SubmitWindow = SubmitWindow()
    ashby_submit: Literal["assisted", "auto"] = "assisted"
    new_window_days: int = Field(7, ge=1)
    min_score: int = Field(70, ge=0, le=100)
    highlight_min_score: int = Field(80, ge=0, le=100)
    watch_keywords: list[str] = []
    allow_remote_us: bool = False
    startup_filter: StartupFilter = StartupFilter()
    cover_letter: Literal["when_required", "always"] = "when_required"
    allow_two_pages: bool = False
    tick_privacy_consent: bool = False
    write_optional_essays: bool = True
    daily_budget_usd: float = Field(5.0, ge=0)
    budget_overrides: dict[date, float] = {}  # one-day limits, e.g. {2026-10-12: 6.0}; other days use daily_budget_usd
    models: Models = Models()
    do_not_apply_folders: list[str] = []
    do_not_apply_folder_ignore: list[str] = []
    jobs_folder_highlight: bool = True
    applications_folder: str = ""  # one folder per application, plus applications.csv ("" = only data/)
    schedule: Schedule = Schedule()
    database_path: str = "data/applier.db"

    def database_file(self, root: Path) -> Path:
        path = Path(self.database_path)
        return path if path.is_absolute() else root / path


class Seniority(StrictModel):
    preferred: list[str] = []
    lower_score: list[str] = []
    excluded: list[str] = []


class Sponsorship(StrictModel):
    skip_if_company_cannot_sponsor: bool = True
    show_lca_history: bool = True
    show_everify_status: bool = True


class Industries(StrictModel):
    preferred: list[str] = []
    open: list[str] = []
    excluded: list[str] = []
    allowed_when_tech: list[str] = []


class Targets(StrictModel):
    role_families: dict[str, list[str]]
    seniority: Seniority = Seniority()
    industries: Industries = Industries()
    must_have_keywords: list[str] = []
    exclude_title_keywords: list[str] = []
    exclude_description_keywords: list[str] = []
    salary_floor_usd: int = Field(0, ge=0)
    sponsorship: Sponsorship = Sponsorship()


def _read_yaml(path: Path) -> dict:
    if not path.exists():
        raise ConfigError(f"Settings file not found: {path}")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path.name} is not valid YAML (check indentation and quotes): {exc}") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(f"{path.name} should contain named settings, one per line")
    return data


def _explain(path: Path, exc: ValidationError) -> ConfigError:
    lines = [f"{path.name} has a problem:"]
    for err in exc.errors():
        where = ".".join(str(part) for part in err["loc"]) or "(top level)"
        lines.append(f"  - {where}: {err['msg']}")
    return ConfigError("\n".join(lines))


def load_config(path: Path) -> Config:
    try:
        return Config.model_validate(_read_yaml(path))
    except ValidationError as exc:
        raise _explain(path, exc) from None


def load_targets(path: Path) -> Targets:
    try:
        return Targets.model_validate(_read_yaml(path))
    except ValidationError as exc:
        raise _explain(path, exc) from None
