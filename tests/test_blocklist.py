import shutil

from applier import db
from applier.blocklist import sync_company_blocks
from applier.companies import company_key
from tests.conftest import write_csv


def blocks(conn):
    return {row["company_key"]: row for row in conn.execute("SELECT * FROM company_blocks")}


def test_folders_and_lists_are_combined(conn, paths, config):
    report = sync_company_blocks(conn, paths, config)
    found = blocks(conn)

    assert report.healthy
    for name in ["Northwind", "Bluepeak", "Grand Atlantic", "Spark AI", "Owlbright", "Acme Robotics", "Quiet Co"]:
        assert company_key(name) in found, name
    # Folders that are not companies are ignored.
    for name in ["Resume", "Job Search Strategy", "Consulting Prep", "notes"]:
        assert company_key(name) not in found, name


def test_strictest_reason_wins_and_highlight_settings(conn, paths, config):
    sync_company_blocks(conn, paths, config)
    found = blocks(conn)
    # Northwind is both in do_not_apply.csv and a folder: do_not_apply wins.
    assert found["northwind"]["reason"] == "do_not_apply"
    assert found["northwind"]["highlight"] == 1
    assert found[company_key("Quiet Co")]["highlight"] == 0  # highlight column says no
    assert found[company_key("Acme Robotics")]["reason"] == "applied_before"
    assert found[company_key("Acme Robotics")]["highlight"] == 1  # always watched
    assert found["owlbright"]["reason"] == "jobs_folder"


def test_new_folder_blocks_on_next_sync(conn, paths, config, jobs_dir):
    sync_company_blocks(conn, paths, config)
    assert company_key("Fresh Startup") not in blocks(conn)
    (jobs_dir / "Fresh Startup - Startup").mkdir()
    sync_company_blocks(conn, paths, config)
    assert company_key("Fresh Startup") in blocks(conn)


def test_removing_from_a_list_unblocks(conn, paths, config):
    sync_company_blocks(conn, paths, config)
    assert company_key("Quiet Co") in blocks(conn)
    write_csv(paths.do_not_apply_csv, "company,website,highlight,notes", "Northwind,,yes,")
    sync_company_blocks(conn, paths, config)
    assert company_key("Quiet Co") not in blocks(conn)


def test_unreachable_folder_never_unblocks(conn, paths, config, jobs_dir):
    sync_company_blocks(conn, paths, config)
    assert "owlbright" in blocks(conn)
    shutil.rmtree(jobs_dir)  # like the D: drive being disconnected

    report = sync_company_blocks(conn, paths, config)
    assert not report.healthy
    assert "owlbright" in blocks(conn)  # previous blocks are kept
    company = db.upsert_company(conn, "Owlbright", "owlbright")
    try:
        db.create_application(conn, company, "Associate")
        raise AssertionError("application should have been blocked")
    except db.CompanyBlocked:
        pass
