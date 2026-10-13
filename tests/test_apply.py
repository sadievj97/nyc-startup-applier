"""Phase 5: reading forms, answering them, filling them, and never submitting in a dry run.

Uses practice forms served from tests/fixtures/forms on this computer and a stand-in for the Claude API.
"""

import json
import threading
from datetime import datetime, timezone
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import yaml

from applier import db
from applier.apply import answers as A
from applier.apply.fields import FormField, ashby_fields, page_problems
from applier.apply.fill import blocked_submit_events, fill_ashby, guard_dry_run, verify_ashby
from applier.apply.session import StopApplication, approve, assisted, dry_run
from applier.profile import load_profile
from applier.tailor.packet import build_packet, packet_dir
from tests.conftest import FIXTURES, make_company
from tests.test_tailor import BROWSERS, FORECAST, FakeClaude, ids, master, selection  # noqa: F401 (fixture)

FORMS = FIXTURES / "forms"
needs_browser = pytest.mark.skipif(not BROWSERS.exists(), reason="Playwright browser not installed")


# ---------------------------------------------------------------- a practice web server


class _Handler(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):  # noqa: N802
        self.rfile.read(int(self.headers.get("Content-Length") or 0))  # read the whole form before answering
        self.server.posts.append(self.path)
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(b"<h1>Application submitted!</h1><p>Thank you for applying.</p>")


@pytest.fixture
def server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), partial(_Handler, directory=str(FORMS)))
    httpd.posts = []
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield httpd
    httpd.shutdown()


def url(server, page: str) -> str:
    return f"http://127.0.0.1:{server.server_address[1]}/{page}"


@pytest.fixture
def browser_page(monkeypatch):
    import os

    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(BROWSERS)
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        yield browser.new_page()
        browser.close()


# ---------------------------------------------------------------- which questions are yours (rule 2)


def field(label, kind="text", options=(), required=True, section=""):
    return FormField(key=label[:10], label=label, kind=kind, required=required, options=list(options), section=section)


@pytest.mark.parametrize("label,kind,expected", [
    ("Are you an LLM?", "textarea", "ai_question"),
    ("Did you use AI or ChatGPT to write your answers?", "boolean", "ai_question"),
    ("Name", "text", "name_full"),
    ("Are you legally authorized to work in the United States?", "boolean", "work_authorization"),
    ("Will you now or at any point in the future require visa sponsorship?", "boolean", "sponsorship_future"),
    ("Do you require sponsorship to work in the US?", "boolean", "sponsorship_now"),
    ("This role is in office 5 days a week in New York. Are you able to commit?", "boolean", "onsite_commitment"),
    ("What are your compensation expectations?", "text", "salary"),
    ("When can you start?", "text", "start_date"),
    ("Where are you currently based?", "select", "location_current"),
    ("How did you hear about us?", "radio", "how_heard"),
    ("Cover letter", "file", "cover_letter_file"),
    ("Cover letter", "textarea", "cover_letter_text"),
    ("Can we contact you about future opportunities?", "boolean", "future_contact"),
    ("I agree to the privacy notice", "checkboxes", "consent_privacy"),
    ("Gender", "radio", "eeo"),
    ("Why do you want to work here?", "textarea", None),
])
def test_classify(label, kind, expected):
    assert A.classify(field(label, kind)) == expected


def test_match_option_prefers_exact_and_never_invents():
    assert A.match_option(["Yes", "No"], ["Yes"]) == "Yes"
    assert A.match_option(["Male", "Female", "I decline to self-identify"], ["Decline to self-identify", "decline"]) \
        == "I decline to self-identify"
    assert A.match_option(["LinkedIn", "Referral"], ["Company website"]) is None


@pytest.fixture
def profile(paths):
    return load_profile(paths.profile_file)


def answer(f, category, profile, config, posting=None, packet=None):
    return A.profile_answer(f, category, profile, config, posting or {"location": "New York, NY"}, packet or {})


def test_answers_you_own_come_from_profile(profile, config):
    assert answer(field("auth", "boolean", ["Yes", "No"]), "work_authorization", profile, config).value == "Yes"
    assert answer(field("now", "boolean", ["Yes", "No"]), "sponsorship_now", profile, config).value == "No"
    assert answer(field("future", "boolean", ["Yes", "No"]), "sponsorship_future", profile, config).value == "Yes"
    assert answer(field("onsite", "boolean", ["Yes", "No"]), "onsite_commitment", profile, config).value == "Yes"
    salary = answer(field("pay"), "salary", profile, config, {"location": "New York, NY", "pay_min": 120000,
                                                                "pay_max": 160000, "pay_currency": "USD",
                                                                "pay_interval": "year"})
    assert salary.value == "$140,000"
    no_range = answer(field("pay"), "salary", profile, config)
    assert no_range.value == "$140,000 to $150,000"
    gender = answer(field("Gender", "radio", ["Male", "Female", "Decline to self-identify"]), "eeo", profile, config)
    assert gender.value == "Decline to self-identify"


def test_questions_your_profile_does_not_cover_need_you(profile, config):
    # In-office commitment outside New York is not covered by your answer.
    onsite = answer(field("onsite", "boolean", ["Yes", "No"]), "onsite_commitment", profile, config,
                    {"location": "San Francisco, CA"})
    assert onsite.status == "needs_me"
    assert answer(field("Are you an LLM?", "textarea"), "ai_question", profile, config).status == "needs_me"
    assert answer(field("citizen", "boolean", ["Yes", "No"]), "citizenship", profile, config).status == "needs_me"
    assert answer(field("start", "date"), "start_date", profile, config).status == "needs_me"
    low = answer(field("pay"), "salary", profile, config, {"location": "New York, NY", "pay_min": 90000,
                                                            "pay_max": 110000, "pay_currency": "USD",
                                                            "pay_interval": "year"})
    assert low.status == "needs_me"
    no_decline = answer(field("Race", "radio", ["Asian", "White"]), "eeo", profile, config)
    assert no_decline.status == "needs_me"


def test_privacy_consent_follows_your_setting(profile, config):
    box = field("I agree to the privacy notice", "checkboxes", ["I agree"])
    assert answer(box, "consent_privacy", profile, config).value == ["I agree"]
    config.tick_privacy_consent = False
    assert answer(box, "consent_privacy", profile, config).status == "needs_me"


def model_reply(*items):
    return {"fields": [dict({"cited_ids": [], "confidence": "high", "worth_answering": True}, **i) for i in items]}


def test_model_cannot_answer_questions_you_own(profile, config, conn, master, monkeypatch):
    """If the model says a question is about sponsorship, code answers it from your profile instead."""
    monkeypatch.setattr(A, "_tailored_lines", lambda t: "")
    fields = [field("Anything we should know about your status?", "boolean", ["Yes", "No"])]
    client = FakeClaude(model_reply({"key": fields[0].key, "category": "sponsorship_future", "answer": "No"}))
    result = A.build_answers(client, conn, config, fields, profile, master, None, {"location": "New York, NY"}, {})
    assert result.answers[0].value == "Yes" and result.answers[0].source == "profile:sponsorship_future"
    request = client.requests[0]
    assert profile.contact.email not in json.dumps(request)  # contact details are never sent to the model


def test_model_answers_are_checked(profile, config, conn, master, monkeypatch):
    monkeypatch.setattr(A, "_tailored_lines", lambda t: "")
    fields = [field("Pick a team", "radio", ["Growth", "Ops"]), field("Biggest win?", "textarea"),
              field("Current employer", "text"), field("Portfolio", "text", required=False)]
    client = FakeClaude(model_reply(
        {"key": fields[0].key, "category": "other", "answer": "Strategy"},       # not an option
        {"key": fields[1].key, "category": "free_text", "answer": "", "confidence": "low"},
        {"key": fields[2].key, "category": "current_company", "answer": "Google"},  # not a current role
        {"key": fields[3].key, "category": "other", "answer": "x", "worth_answering": False},
    ))
    result = A.build_answers(client, conn, config, fields, profile, master, None, {}, {})
    statuses = [a.status for a in result.answers]
    assert statuses == ["needs_me", "needs_me", "needs_me", "skip"]


# ---------------------------------------------------------------- the live page (practice form)


@needs_browser
def test_reads_every_field_of_the_form(server, browser_page):
    browser_page.goto(url(server, "ashby_form.html"))
    fields = {f.key: f for f in ashby_fields(browser_page)}
    assert len(fields) == 13
    assert fields["_systemfield_resume"].kind == "file" and fields["_systemfield_resume"].required
    assert fields["q_auth"].kind == "boolean" and fields["q_auth"].options == ["Yes", "No"]
    assert fields["q_heard"].kind == "radio" and "Company website" in fields["q_heard"].options
    assert fields["q_office"].kind == "checkboxes" and fields["q_office"].options == ["New York", "San Francisco"]
    assert fields["q_why"].kind == "textarea" and not fields["q_why"].required
    assert fields["q_based"].kind == "select"
    assert fields["eeo_gender"].section == "eeo"
    assert page_problems(browser_page) == []


@needs_browser
@pytest.mark.parametrize("page,expected", [("captcha.html", "CAPTCHA"), ("verify_email.html", "the page says")])
def test_bot_checks_are_detected(server, browser_page, page, expected):
    browser_page.goto(url(server, page))
    browser_page.wait_for_timeout(500)
    problems = page_problems(browser_page)
    assert problems and expected in problems[0]


@needs_browser
def test_safety_net_blocks_a_submission(server, browser_page):
    guard = guard_dry_run(browser_page)
    browser_page.goto(url(server, "ashby_form.html"))
    browser_page.click("#submit")
    browser_page.wait_for_timeout(800)
    assert server.posts == []  # the practice server never received anything
    assert len(guard.blocked) == 1
    assert browser_page.get_attribute("body", "data-submit-failed") == "1"


@needs_browser
def test_fill_and_read_back(server, browser_page, tmp_path):
    resume = tmp_path / "Test_Person_Resume_Acme.pdf"
    resume.write_bytes(b"%PDF-1.4 practice")
    browser_page.goto(url(server, "ashby_form.html"))
    answers = [
        A.Answer("_systemfield_resume", "Resume", "file", "resume_file", str(resume)),
        A.Answer("_systemfield_name", "Name", "text", "name_full", "Test Person"),
        A.Answer("q_auth", "auth", "boolean", "work_authorization", "Yes"),
        A.Answer("q_sponsor", "sponsor", "boolean", "sponsorship_future", "No"),
        A.Answer("q_heard", "heard", "radio", "how_heard", "Company website"),
        A.Answer("q_office", "office", "checkboxes", "office_choice", ["New York"]),
        A.Answer("q_why", "why", "textarea", "free_text", "I build forecasting models."),
        A.Answer("q_based", "based", "select", "location_current", "Boston, MA"),
    ]
    assert fill_ashby(browser_page, answers) == []
    assert verify_ashby(browser_page, answers) == []
    assert answers[-1].value == "Boston, Massachusetts, United States"  # not Boston, Virginia
    # A value that did not stick is reported.
    browser_page.fill("#name", "Someone Else")
    assert any("Name" in p for p in verify_ashby(browser_page, answers))


# ---------------------------------------------------------------- whole dry run and assisted mode


@pytest.fixture
def form_posting(conn, config, paths, master, server, monkeypatch):
    from applier.resume.model import save_master

    monkeypatch.setattr("applier.paths.Paths.browsers_dir", property(lambda self: BROWSERS))
    save_master(paths.master_file, master)
    company = make_company(conn, "Acme Labs")
    with conn:
        cur = conn.execute(
            """INSERT INTO postings (company_id, ats, ats_posting_id, title, location, description_text, url, apply_url)
               VALUES (?, 'ashby', 'x1', 'Data Analyst', 'New York, NY', 'Forecasting and analytics role.', ?, ?)""",
            (company, url(server, "ashby_form.html"), url(server, "ashby_form.html")))
    posting = cur.lastrowid
    build_packet(FakeClaude(selection(master)), conn, config, paths, posting)
    return posting


def essay_replies(master):
    variant = ids(master)[FORECAST][1]
    return [model_reply({"key": "q_why", "category": "free_text", "cited_ids": [variant],
                         "answer": "I built a forecasting model covering 40 stores that cut stockouts by 12%."}),
            {"items": [{"id": "e0", "supported": True, "problem": ""}]}]


@needs_browser
def test_dry_run_fills_everything_and_submits_nothing(conn, config, paths, form_posting, master, server):
    result = dry_run(FakeClaude(*essay_replies(master)), conn, config, paths, form_posting, headless=True)
    assert result.problems == []
    assert len(result.needs_me) == 1 and "Are you an LLM?" in result.needs_me[0]
    assert server.posts == []
    assert Path(result.screenshots[0]).exists()
    saved = {a["key"]: a for a in json.loads((packet_dir(paths, "Acme Labs", form_posting) / "answers.json").read_text())}
    assert saved["q_auth"]["value"] == "Yes" and saved["q_sponsor"]["value"] == "Yes"
    assert saved["q_onsite"]["value"] == "Yes" and saved["q_office"]["value"] == ["New York"]
    assert saved["q_heard"]["value"] == "Company website"
    assert saved["q_salary"]["value"] == "$140,000 to $150,000"
    assert saved["eeo_gender"]["value"] == "Decline to self-identify"
    assert saved["q_why"]["source"] == "model" and saved["q_why"]["status"] == "ok"
    assert "YOU ANSWER THIS" in result.answers_file.read_text(encoding="utf-8")
    assert conn.execute("SELECT COUNT(*) FROM applications").fetchone()[0] == 0  # a test dry run queues nothing
    assert conn.execute("SELECT COUNT(*) FROM flags WHERE kind = 'unanswerable_question'").fetchone()[0] == 1


@needs_browser
def test_unsupported_essay_is_not_used(conn, config, paths, form_posting, master):
    bad = {"items": [{"id": "e0", "supported": False, "problem": "40 stores is not in the sources"}]}
    replies = essay_replies(master)
    replies[1] = bad
    revised = {"answer": "I built a forecasting model covering 40 stores.", "cited_ids": []}
    result = dry_run(FakeClaude(*replies, revised, bad), conn, config, paths, form_posting, headless=True)
    saved = {a["key"]: a for a in json.loads((packet_dir(paths, "Acme Labs", form_posting) / "answers.json").read_text())}
    assert saved["q_why"]["status"] == "skip" and "fact-check" in saved["q_why"]["reason"]
    assert result.problems == []
    assert conn.execute("SELECT COUNT(*) FROM flags WHERE kind = 'fact_check_failed'").fetchone()[0] == 1


@needs_browser
def test_captcha_stops_the_application_and_flags_it(conn, config, paths, form_posting, server):
    with conn:
        conn.execute("UPDATE postings SET apply_url = ? WHERE id = ?", (url(server, "captcha.html"), form_posting))
    result = dry_run(FakeClaude(), conn, config, paths, form_posting, headless=True)
    assert result.problems and "CAPTCHA" in result.problems[0]
    assert conn.execute("SELECT COUNT(*) FROM flags WHERE kind = 'bot_check'").fetchone()[0] == 1
    assert Path(result.screenshots[0]).exists()  # the packet and a screenshot are kept for you
    assert not (packet_dir(paths, "Acme Labs", form_posting) / "answers.json").exists()


IN_WINDOW = datetime(2026, 10, 13, 15, 0, tzinfo=timezone.utc)  # 11:00 in New York


@needs_browser
def test_assisted_counts_only_after_the_confirmation_page(conn, config, paths, form_posting, master, server):
    first = dry_run(FakeClaude(*essay_replies(master)), conn, config, paths, form_posting, queue=True, headless=True)
    app_id = first.application_id
    assert first.status == "needs_me"  # "Are you an LLM?" is yours to answer

    config.mode = "review"
    with pytest.raises(StopApplication, match="approval"):
        assisted(FakeClaude(), conn, config, paths, app_id, now=IN_WINDOW, headless=True)
    notes = approve(conn, config, app_id)
    assert notes and "browser" in notes[0]

    # The window closes without a confirmation page: nothing counts as submitted.
    result = assisted(FakeClaude(*essay_replies(master)), conn, config, paths, app_id, now=IN_WINDOW,
                      wait_minutes=0.1, headless=True)
    assert not result.submitted and server.posts == []
    assert conn.execute("SELECT status FROM applications WHERE id = ?", (app_id,)).fetchone()[0] == "prepared"

    # "You" click Submit (the practice page does it), the confirmation page appears: now it counts.
    with conn:
        conn.execute("UPDATE postings SET apply_url = ? WHERE id = ?", (url(server, "ashby_form.html?confirm"), form_posting))
    result = assisted(FakeClaude(*essay_replies(master)), conn, config, paths, app_id, now=IN_WINDOW,
                      wait_minutes=1, headless=True)
    assert result.submitted and len(server.posts) == 1
    row = conn.execute("SELECT status, submitted_at, mode FROM applications WHERE id = ?", (app_id,)).fetchone()
    assert row["status"] == "submitted" and row["submitted_at"] and row["mode"] == "assisted"


def test_assisted_refuses_in_dry_run_mode(conn, config, paths):
    company = make_company(conn, "Beta Labs")
    app_id = db.create_application(conn, company, "Analyst", ats="ashby", status="prepared")
    with pytest.raises(StopApplication, match="dry_run"):
        assisted(FakeClaude(), conn, config, paths, app_id, now=IN_WINDOW, headless=True)


def test_approve_only_prepared_applications(conn, config):
    company = make_company(conn, "Gamma Labs")
    app_id = db.create_application(conn, company, "Analyst", ats="greenhouse", status="queued")
    with pytest.raises(StopApplication, match="queued"):
        approve(conn, config, app_id)
    db.set_status(conn, app_id, "needs_me")
    with pytest.raises(StopApplication, match="need you"):  # an agent-submitted form cannot leave questions open
        approve(conn, config, app_id)


# ---------------------------------------------------------------- Greenhouse and Lever


GH_QUESTIONS = [
    {"label": "First Name", "required": True, "fields": [{"name": "first_name", "type": "input_text", "values": []}]},
    {"label": "Resume/CV", "required": True, "fields": [{"name": "resume", "type": "input_file", "values": []},
                                                          {"name": "resume_text", "type": "textarea", "values": []}]},
    {"label": "Are you legally authorized to work in the US?", "required": True,
     "fields": [{"name": "question_3", "type": "multi_value_single_select",
                 "values": [{"label": "Yes", "value": 1}, {"label": "No", "value": 0}]}]},
]


@pytest.fixture
def make_posting(conn, config, paths, master, server, monkeypatch):
    """A posting on one of the practice forms, with a packet that passed the fact-check."""
    from applier.resume.model import save_master

    monkeypatch.setattr("applier.paths.Paths.browsers_dir", property(lambda self: BROWSERS))
    save_master(paths.master_file, master)

    def make(ats, page, name, extra=None):
        company = make_company(conn, name)
        with conn:
            cur = conn.execute(
                """INSERT INTO postings (company_id, ats, ats_posting_id, title, location, description_text, url,
                                         apply_url, extra_json)
                   VALUES (?, ?, 'x1', 'Data Analyst', 'New York, NY', 'Forecasting and analytics role.', ?, ?, ?)""",
                (company, ats, url(server, page), url(server, page), json.dumps(extra or {})))
        build_packet(FakeClaude(selection(master)), conn, config, paths, cur.lastrowid)
        return cur.lastrowid
    return make


@needs_browser
def test_greenhouse_reader_cross_checks_the_question_list(server, browser_page):
    from applier.apply.fields import greenhouse_fields

    browser_page.goto(url(server, "greenhouse_form.html"))
    missing = GH_QUESTIONS + [{"label": "Portfolio", "required": True,
                               "fields": [{"name": "question_9", "type": "input_text", "values": []}]}]
    fields, problems = greenhouse_fields(browser_page, missing)
    by_key = {f.key: f for f in fields}
    assert problems == ["the page is missing 'Portfolio' from Greenhouse's question list"]
    assert by_key["resume"].kind == "file" and by_key["resume"].label == "Resume/CV" and by_key["resume"].required
    assert "resume_text" not in by_key  # the hidden "enter manually" box is not a separate question
    assert by_key["question_3"].options == ["Yes", "No"]
    assert by_key["country"].options[1] == "United States +1"  # read by opening the dropdown
    assert by_key["gender"].section == "eeo"
    assert not by_key["cover_letter"].required


@needs_browser
def test_greenhouse_dry_run(conn, config, paths, make_posting, server):
    posting = make_posting("greenhouse", "greenhouse_form.html", "Coastal Labs", {"questions": GH_QUESTIONS})
    result = dry_run(FakeClaude(), conn, config, paths, posting, headless=True)  # every question is yours: no model call
    assert result.problems == []
    assert len(result.needs_me) == 1 and "currently based in NYC" in result.needs_me[0]
    assert server.posts == []
    saved = {a["key"]: a for a in json.loads((packet_dir(paths, "Coastal Labs", posting) / "answers.json").read_text())}
    assert saved["country"]["value"] == "United States +1"  # not "United States Minor Outlying Islands +1"
    assert saved["question_3"]["value"] == "Yes" and saved["question_4"]["value"] == "Yes"
    assert saved["gender"]["value"] == "Decline To Self Identify"
    assert saved["cover_letter"]["status"] == "skip"


@needs_browser
def test_lever_dry_run(conn, config, paths, make_posting, master, server):
    posting = make_posting("lever", "lever_form.html", "Acme Mobile")
    reply = model_reply({"key": "org", "category": "current_company", "answer": "", "confidence": "low"},
                        {"key": "urls[Other]", "category": "other", "answer": "", "worth_answering": False})
    result = dry_run(FakeClaude(reply), conn, config, paths, posting, headless=True)
    assert result.problems == []
    assert len(result.needs_me) == 1 and "Canada" in result.needs_me[0]  # your answers cover the US only
    assert server.posts == []
    saved = {a["key"]: a for a in json.loads((packet_dir(paths, "Acme Mobile", posting) / "answers.json").read_text())}
    assert saved["name"]["value"] == "Test Person" and saved["location"]["value"] == "Boston, MA, USA"
    assert saved["cards[a1][field1]"]["value"] == "Yes"
    assert saved["cards[b2][field0]"]["value"] == "$140,000 to $150,000"
    assert saved["eeo[gender]"]["value"] == "Decline to self-identify"
    assert saved["urls[Other]"]["category"] != "phone"  # a label like "Acme Mobile" is not a phone question


@needs_browser
@pytest.mark.parametrize("page,button", [("greenhouse_form.html", "#submit"), ("lever_form.html", "#btn-submit")])
def test_safety_net_blocks_greenhouse_and_lever_submissions(server, browser_page, page, button):
    guard = guard_dry_run(browser_page, ["/" + page])
    browser_page.goto(url(server, page))
    browser_page.click(button)
    browser_page.wait_for_timeout(800)
    assert server.posts == []
    assert guard.blocked or blocked_submit_events(browser_page) == 1


def test_other_country_authorization_needs_you(profile, config):
    canada = field("Are you authorized to work lawfully in Canada for Acme Inc?", "radio", ["Yes", "No"])
    assert answer(canada, A.classify(canada), profile, config).status == "needs_me"
    uk = field("Will you require sponsorship to work in the UK?", "radio", ["Yes", "No"])
    assert answer(uk, A.classify(uk), profile, config).status == "needs_me"
    us = field("Are you authorized to work lawfully in the US for Acme Mobile Inc?", "radio", ["Yes", "No"])
    assert answer(us, A.classify(us), profile, config).value == "Yes"


def test_places_must_match_city_and_state():
    from applier.apply.fill import _best

    options = ["Boston, Virginia, United States", "Boston, Massachusetts, United States", "Bostonia, CA, USA"]
    assert options[_best(options, "Boston, MA")] == "Boston, Massachusetts, United States"
    assert _best(["Boston, Virginia, United States"], "Boston, MA") is None  # no wrong place: it goes to you
    countries = ["United States Minor Outlying Islands +1", "United States +1"]
    assert countries[_best(countries, "United States")] == "United States +1"
    assert _best(["Yes", "No"], "Yes") == 0


# ---------------------------------------------------------------- your copy of each application


def test_application_folders_and_csv(conn, config, paths, jobs_dir):
    from applier.apply.folders import folder_name, sync_folders
    from applier.companies import read_company_folders

    config.applications_folder = str(jobs_dir / "Claude Applications")
    company = make_company(conn, "Bright Labs")
    packet = paths.root / "packets" / "Bright_Labs_1"
    (packet / "screenshots").mkdir(parents=True)
    (packet / "Test_Person_Resume_Bright_Labs.pdf").write_bytes(b"%PDF-1.4 practice")
    (packet / "answers.md").write_text("answers", encoding="utf-8")
    (packet / "screenshots" / "dry_run_1.png").write_bytes(b"png")
    app_id = db.create_application(conn, company, "Ops / Strategy: Lead?", ats="ashby", status="prepared")
    with conn:
        conn.execute("UPDATE applications SET packet_dir = ? WHERE id = ?", (str(packet), app_id))

    assert sync_folders(conn, config, paths) == []
    folder = jobs_dir / "Claude Applications" / folder_name("Bright Labs", "Ops / Strategy: Lead?")
    assert folder.name == "Bright Labs - Ops Strategy Lead"  # characters Windows forbids are removed
    assert (folder / "Test_Person_Resume_Bright_Labs.pdf").exists() and (folder / "answers.md").exists()
    assert (folder / "screenshots" / "dry_run_1.png").exists()
    assert "Status: prepared" in (folder / "Application.md").read_text(encoding="utf-8")
    for csv_file in (jobs_dir / "Claude Applications" / "applications.csv", paths.data_dir / "applications.csv"):
        assert "Bright Labs" in csv_file.read_text(encoding="utf-8-sig")

    # The agent's own folder inside the Jobs folder is never read as a company you are talking to.
    names = [c.name for c in read_company_folders(jobs_dir, config.do_not_apply_folder_ignore, True)]
    assert "Claude Applications" not in names and "Northwind" in names


def test_no_application_is_created_when_the_packet_cannot_be_made(conn, config, paths, master):
    from applier.llm import BudgetReached
    from applier.resume.model import save_master

    save_master(paths.master_file, master)

    company = make_company(conn, "Budget Labs")
    with conn:
        posting = conn.execute("INSERT INTO postings (company_id, ats, ats_posting_id, title, url) "
                               "VALUES (?, 'ashby', 'b1', 'Ops', 'https://x')", (company,)).lastrowid
    config.daily_budget_usd = 0.0  # the budget is used up: the tailored resume cannot be written
    with pytest.raises(BudgetReached):
        dry_run(FakeClaude(), conn, config, paths, posting, queue=True, headless=True)
    assert conn.execute("SELECT COUNT(*) FROM applications").fetchone()[0] == 0


@needs_browser
def test_flagged_essay_is_revised_once_and_then_reused(conn, config, paths, form_posting, master):
    variant = ids(master)[FORECAST][1]
    replies = essay_replies(master)
    replies[1] = {"items": [{"id": "e0", "supported": False, "problem": "the cut in stockouts is overstated"}]}
    revised = {"answer": "I built a forecasting model covering 40 stores.", "cited_ids": [variant]}
    ok = {"items": [{"id": "e0", "supported": True, "problem": ""}]}
    client = FakeClaude(*replies, revised, ok)
    dry_run(client, conn, config, paths, form_posting, headless=True)
    saved = {a["key"]: a for a in json.loads((packet_dir(paths, "Acme Labs", form_posting) / "answers.json").read_text())}
    assert saved["q_why"]["status"] == "ok" and saved["q_why"]["value"] == "I built a forecasting model covering 40 stores."
    assert "CHECKER FOUND" in client.requests[2]["messages"][0]["content"]
    # The next fill reuses the checked answer you previewed: no new essay, no model call.
    again = FakeClaude()
    dry_run(again, conn, config, paths, form_posting, headless=True)
    saved = {a["key"]: a for a in json.loads((packet_dir(paths, "Acme Labs", form_posting) / "answers.json").read_text())}
    assert again.requests == [] and saved["q_why"]["value"] == "I built a forecasting model covering 40 stores."
