from applier.cli import main


def run(paths, capsys, *args):
    code = main(["--root", str(paths.root), *args])
    return code, capsys.readouterr().out


def test_status_reports_settings_and_why_it_cannot_submit(paths, capsys):
    code, out = run(paths, capsys, "status")
    assert code == 0
    assert "dry_run" in out
    assert "No, because" in out
    assert "mode is dry_run" in out
    assert "is valid" in out
    assert "Blocked companies" in out


def test_status_never_shows_the_key(paths, capsys):
    secret = "sk-ant-api03-" + "z" * 40
    paths.env_file.write_text(f"ANTHROPIC_API_KEY={secret}\n", encoding="utf-8")
    code, out = run(paths, capsys, "status")
    assert "filled in" in out
    assert secret not in out


def test_check_company(paths, capsys):
    _, out = run(paths, capsys, "check-company", "Spark AI")
    assert "will NOT apply" in out
    _, out = run(paths, capsys, "check-company", "Unknown New Startup")
    assert "may apply" in out


def test_pause_and_resume(paths, capsys):
    run(paths, capsys, "pause")
    assert paths.pause_file.exists()
    _, out = run(paths, capsys, "status")
    assert "PAUSE file exists" in out
    run(paths, capsys, "resume")
    assert not paths.pause_file.exists()


def test_reset_needs_confirmation(paths, capsys, conn):
    from applier import db
    from tests.conftest import make_company

    company = make_company(conn, "Bright Labs")
    db.create_application(conn, company, "Associate")
    code, out = run(paths, capsys, "reset-company", "Bright Labs")
    assert code == 1 and "--yes" in out
    assert db.existing_application(conn, "brightlabs") is not None
    code, out = run(paths, capsys, "reset-company", "Bright Labs", "--yes")
    assert code == 0 and db.existing_application(conn, "brightlabs") is None


def test_bad_settings_give_a_plain_message(paths, capsys):
    paths.config_file.write_text("mode: yolo\n", encoding="utf-8")
    code, out = run(paths, capsys, "status")
    assert code == 2
    assert "config.yaml has a problem" in out
