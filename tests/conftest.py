"""Shared test setup: a throwaway project folder with the real settings files and fake personal data."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml

from applier import db
from applier.config import Config, load_config
from applier.paths import PROJECT_ROOT, Paths

FIXTURES = Path(__file__).parent / "fixtures"


def write_csv(path: Path, header: str, *rows: str) -> None:
    path.write_text("\n".join([header, *rows]) + "\n", encoding="utf-8")


@pytest.fixture
def jobs_dir(tmp_path: Path) -> Path:
    """A stand-in for your Jobs folder."""
    jobs = tmp_path / "Jobs"
    for name in ["0. Resume", "1. Job Search Strategy", "Consulting Prep", "Northwind", "Bluepeak - AI",
                 "Grand Atlantik", "SparkAI - Startup", "Owlbright"]:
        (jobs / name).mkdir(parents=True)
    (jobs / "notes.txt").write_text("not a folder", encoding="utf-8")
    return jobs


@pytest.fixture
def paths(tmp_path: Path, jobs_dir: Path) -> Paths:
    root = tmp_path / "project"
    config_dir = root / "config"
    config_dir.mkdir(parents=True)

    settings = yaml.safe_load((PROJECT_ROOT / "config" / "config.yaml").read_text(encoding="utf-8"))
    settings["do_not_apply_folders"] = [str(jobs_dir)]
    settings.pop("budget_overrides", None)  # a one-day limit you set today must not change the tests
    settings["mode"] = "dry_run"  # tests start safe whatever mode your real settings use
    settings["do_not_apply_folder_ignore"] = ["Consulting Prep", "Claude Applications"]  # the practice Jobs folder's
    settings["applications_folder"] = str(tmp_path / "Claude Applications")  # never your real folder
    (config_dir / "config.yaml").write_text(yaml.safe_dump(settings), encoding="utf-8")
    shutil.copyfile(PROJECT_ROOT / "config" / "targets.yaml", config_dir / "targets.yaml")
    shutil.copyfile(PROJECT_ROOT / ".env.example", root / ".env.example")

    write_csv(config_dir / "companies.csv", "name,website,careers_url,ats,board_token,notes")
    write_csv(config_dir / "applied_before.csv", "company,website,applied_on,role,source,notes",
              "Acme Robotics Inc.,https://acme.example,2026-03-01,Associate,tracker,")
    write_csv(config_dir / "do_not_apply.csv", "company,website,highlight,notes",
              "Northwind,https://northwind.com,yes,talking already",
              "Quiet Co,,no,")
    write_csv(config_dir / "company_aliases.csv", "alias,canonical", "Grand Atlantik,Grand Atlantic")

    (root / "profile").mkdir()
    shutil.copyfile(FIXTURES / "profile.yaml", root / "profile" / "profile.yaml")
    return Paths(root)


@pytest.fixture
def config(paths: Paths) -> Config:
    return load_config(paths.config_file)


@pytest.fixture
def conn(paths: Paths, config: Config):
    connection = db.open_db(config.database_file(paths.root))
    yield connection
    connection.close()


def make_company(conn, name: str = "Bright Labs", **kwargs) -> int:
    from applier.companies import company_key

    return db.upsert_company(conn, name, company_key(name), **kwargs)
