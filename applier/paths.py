"""Where everything lives. All paths hang off one project root so tests can use a temporary copy."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Paths:
    root: Path

    @property
    def config_dir(self) -> Path:
        return self.root / "config"

    @property
    def config_file(self) -> Path:
        return self.config_dir / "config.yaml"

    @property
    def targets_file(self) -> Path:
        return self.config_dir / "targets.yaml"

    @property
    def companies_csv(self) -> Path:
        return self.config_dir / "companies.csv"

    @property
    def applied_before_csv(self) -> Path:
        return self.config_dir / "applied_before.csv"

    @property
    def do_not_apply_csv(self) -> Path:
        return self.config_dir / "do_not_apply.csv"

    @property
    def aliases_csv(self) -> Path:
        return self.config_dir / "company_aliases.csv"

    @property
    def profile_dir(self) -> Path:
        return self.root / "profile"

    @property
    def profile_file(self) -> Path:
        return self.profile_dir / "profile.yaml"

    @property
    def source_dir(self) -> Path:
        return self.profile_dir / "source"

    @property
    def extracted_file(self) -> Path:
        return self.profile_dir / "extracted.yaml"

    @property
    def groups_file(self) -> Path:
        return self.profile_dir / "resume_groups.yaml"

    @property
    def master_file(self) -> Path:
        return self.profile_dir / "master_resume.yaml"

    @property
    def review_file(self) -> Path:
        return self.profile_dir / "review" / "master_resume_review.html"

    @property
    def rendered_dir(self) -> Path:
        return self.profile_dir / "rendered"

    @property
    def browsers_dir(self) -> Path:
        return self.root / ".playwright-browsers"

    @property
    def data_dir(self) -> Path:
        return self.root / "data"

    @property
    def logs_dir(self) -> Path:
        return self.root / "logs"

    @property
    def pause_file(self) -> Path:
        return self.root / "PAUSE"

    @property
    def env_file(self) -> Path:
        return self.root / ".env"

    @property
    def env_example_file(self) -> Path:
        return self.root / ".env.example"


def default_paths() -> Paths:
    return Paths(PROJECT_ROOT)
