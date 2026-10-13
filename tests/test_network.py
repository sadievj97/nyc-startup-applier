"""Phase 6: highlights, people from public professional sources, outreach drafts, digest, dashboard."""

import json
from types import SimpleNamespace as NS

import pytest

from applier import db
from applier.blocklist import sync_company_blocks
from applier.network import find_highlights, find_people, linkedin_search_url, note_problems
from tests.conftest import make_company
from tests.test_tailor import master  # noqa: F401 (fixture)


class FakeSearch:
    """Stands in for one web-search answer: the URLs it 'saw' and the final JSON text."""

    def __init__(self, urls, answer):
        self.urls, self.answer, self.requests = urls, answer, []
        self.beta = NS(messages=NS(create=self._create))

    def _create(self, **kwargs):
        self.requests.append(kwargs)
        results = NS(type="web_search_tool_result", content=[NS(url=u) for u in self.urls], citations=None)
        text = NS(type="text", text=json.dumps(self.answer), citations=None)
        return NS(content=[results, text], stop_reason="end_turn", model=kwargs["model"],
                  usage=NS(input_tokens=2000, output_tokens=300, cache_read_input_tokens=0,
                           cache_creation_input_tokens=0, server_tool_use=NS(web_search_requests=2)))


def posting(conn, company_id, title, score, decision="eligible", location="New York, NY"):
    with conn:
        cur = conn.execute("""INSERT INTO postings (company_id, ats, ats_posting_id, title, location, url, score, decision)
                              VALUES (?, 'ashby', ?, ?, ?, ?, ?, ?)""",
                           (company_id, title, title, location, f"https://jobs.example/{title.replace(' ', '-')}", score, decision))
    return cur.lastrowid


def test_applied_before_company_gets_a_highlight_with_public_people_only(conn, config, paths):
    """Acceptance test: no application at a company in applied_before.csv, but a strong new posting there is a
    highlight, with people from public professional sources only and no contact details."""
    from applier.jobs.store import choose_per_company

    sync_company_blocks(conn, paths, config)  # applied_before.csv lists Acme Robotics Inc.
    company = make_company(conn, "Acme Robotics", website="https://acme.example")
    strong = posting(conn, company, "Chief of Staff", 88)
    weak = posting(conn, company, "Office Coordinator", 50)
    far = posting(conn, company, "Strategy Lead", 90, decision="filtered_location", location="London")
    choose_per_company(conn, config)
    assert conn.execute("SELECT decision FROM postings WHERE id = ?", (strong,)).fetchone()[0] == "blocked_company"
    assert conn.execute("SELECT COUNT(*) FROM applications").fetchone()[0] == 0  # never applied

    find_highlights(conn, config)
    rows = conn.execute("SELECT posting_id FROM highlights").fetchall()
    assert [r[0] for r in rows] == [strong]  # not the weak one, not the one outside New York
    assert far and weak

    company_row = conn.execute("SELECT * FROM companies WHERE id = ?", (company,)).fetchone()
    posting_row = conn.execute("SELECT * FROM postings WHERE id = ?", (strong,)).fetchone()
    answer = {"people": [
        {"name": "Jane Rivera", "title": "COO", "source_url": "https://acme.example/team", "why": "leads operations"},
        {"name": "Tom Lee", "title": "CEO", "source_url": "https://www.linkedin.com/in/tomlee", "why": "founder"},
        {"name": "Ann Ko", "title": "Head of People", "source_url": "https://news.example/acme-raises",
         "why": "reach her at ann@acme.example"},
        {"name": "Bo Chen", "title": "VP", "source_url": "https://unseen.example/page", "why": "named somewhere"},
    ]}
    client = FakeSearch(["https://acme.example/team", "https://news.example/acme-raises", "https://www.linkedin.com/in/tomlee"], answer)
    outcome = find_people(client, conn, config, company_row, posting_row)
    tool = client.requests[0]["tools"][0]
    assert "linkedin.com" in tool["blocked_domains"]  # the search itself never uses LinkedIn
    people = conn.execute("SELECT * FROM people").fetchall()
    assert [p["name"] for p in people] == ["Jane Rivera"]
    assert len(outcome.rejected) == 3  # LinkedIn source, contact details, and a source not in the search results
    assert set(people[0].keys()) >= {"name", "title", "source_url", "why"} and "email" not in people[0].keys()
    stored = json.loads(conn.execute("SELECT people_json FROM highlights").fetchone()[0])
    assert stored == [{"name": "Jane Rivera", "title": "COO", "source_url": "https://acme.example/team", "why": "leads operations"}]
    assert linkedin_search_url("Jane Rivera", "Acme Robotics").startswith("https://www.linkedin.com/search/results/people/?keywords=Jane%20Rivera")


def test_better_fit_than_the_applied_role_is_a_highlight(conn, config):
    company = make_company(conn, "Bright Labs")
    applied_posting = posting(conn, company, "Analyst", 72, decision="blocked_company")
    better = posting(conn, company, "Chief of Staff", 78, decision="blocked_company")
    db.create_application(conn, company, "Analyst", posting_id=applied_posting, status="queued")
    find_highlights(conn, config)
    reason = conn.execute("SELECT reason FROM highlights WHERE posting_id = ?", (better,)).fetchone()[0]
    assert "fits better than the role you applied for" in reason


@pytest.mark.parametrize("body,ok", [
    ("i applied for the Chief of Staff role today. Would you have 15 minutes next week?", True),
    ("I applied for the role today. Would you have 15 minutes next week?", False),  # capital I
    ("i'm excited about the role. Could we talk?", False),  # contraction
    ("i applied today — could we talk?", False),  # em dash
    ("i applied. i am excited. Could we talk? Thanks.", False),  # more than two sentences
    ("i applied today; reach me at 617 555 0100.", False),  # contact details
    ("i led pricing research. would you have 15 minutes?", False),  # a sentence starts in lowercase
    ("i'd like to talk.", False),  # contraction
    ("My name is Test. i would value 15 minutes next week.", True),
])
def test_outreach_note_rules(body, ok):
    assert (note_problems(body) == []) is ok


def test_digest_page(conn, config, paths):
    from applier.digest import write_digest

    company = make_company(conn, "Bright Labs")
    db.add_flag(conn, "interview_request", "Bright Labs: <b>interview</b> request", severity="urgent", company_id=company)
    out = write_digest(conn, config, paths)
    html = out.read_text(encoding="utf-8")
    assert "Urgent (1)" in html and "&lt;b&gt;interview&lt;/b&gt;" in html  # outside text is escaped
    assert (paths.root / "digest" / "latest.html").exists()


def test_urgent_flags_are_notified_once(conn, paths):
    from applier.notify import notify_urgent

    db.add_flag(conn, "offer", "Bright Labs: offer", severity="urgent")
    db.add_flag(conn, "other", "something normal")
    shown = []
    assert notify_urgent(conn, paths, send=lambda title, text: shown.append(text) or True) == 1
    assert notify_urgent(conn, paths, send=lambda title, text: shown.append(text) or True) == 0
    assert shown == ["Bright Labs: offer"]


def test_dashboard_pages_and_token(conn, config, paths):
    from applier.dashboard import create_app

    company = make_company(conn, "Bright Labs")
    app_id = db.create_application(conn, company, "Analyst", ats="ashby", status="queued")
    db.set_status(conn, app_id, "prepared")
    app = create_app(paths)
    client = app.test_client()
    for route in ("/", "/review", "/applications", "/highlights", "/networking", "/flags", "/emails"):
        assert client.get(route, environ_base={"REMOTE_ADDR": "127.0.0.1"}).status_code == 200
    # Another computer cannot open it, and a form without the secret token is refused.
    assert client.get("/", environ_base={"REMOTE_ADDR": "10.0.0.5"}).status_code == 403
    assert client.post("/action/approve", data={"id": app_id}, environ_base={"REMOTE_ADDR": "127.0.0.1"}).status_code == 403
    token = app.config["ACTION_TOKEN"]
    response = client.post("/action/approve", data={"id": app_id, "token": token}, environ_base={"REMOTE_ADDR": "127.0.0.1"})
    assert response.status_code == 302
    assert conn.execute("SELECT approved_at FROM applications WHERE id = ?", (app_id,)).fetchone()[0]


def test_dashboard_skip_keeps_the_posting_out(conn, config, paths):
    from applier.dashboard import create_app
    from applier.jobs.store import choose_per_company

    company = make_company(conn, "Bright Labs")
    p = posting(conn, company, "Chief of Staff", 90)
    app = create_app(paths)
    token = app.config["ACTION_TOKEN"]
    app.test_client().post("/action/skip_posting", data={"id": p, "token": token}, environ_base={"REMOTE_ADDR": "127.0.0.1"})
    with conn:
        conn.execute("UPDATE postings SET decision = 'eligible' WHERE id = ?", (p,))  # as after re-scoring
    choose_per_company(conn, config)
    assert conn.execute("SELECT decision FROM postings WHERE id = ?", (p,)).fetchone()[0] == "manual"


def test_flagged_note_gets_one_revision(conn, config, paths, master):
    from applier.network import draft_note
    from applier.resume.model import save_master
    from tests.test_tailor import FORECAST, FakeClaude, ids

    save_master(paths.master_file, master)
    company = make_company(conn, "Bright Labs")
    with conn:
        conn.execute("INSERT INTO people (company_id, name, title, source_url, why) VALUES (?, 'Dana Reyes', 'CEO', "
                     "'https://brightlabs.example/team', 'runs the company')", (company,))
    person = conn.execute("SELECT * FROM people").fetchone()
    cited = [ids(master)[FORECAST][1]]
    client = FakeClaude(
        {"body": "i am a student who built a forecasting model. Could we talk next week?", "cited_ids": cited},
        {"items": [{"id": "n0", "supported": False, "problem": "student is not supported: the degree has ended"}]},
        {"body": "i built a forecasting model covering 40 stores. Could we talk for 15 minutes next week?", "cited_ids": cited},
        {"items": [{"id": "n0", "supported": True, "problem": ""}]},
    )
    result = draft_note(client, conn, config, paths, person)
    assert result.status == "checked" and "covering 40 stores" in result.note
    assert result.note.startswith("Hi Dana,") and result.note.endswith("Best,\nTest")
    assert "CHECKER FOUND" in client.requests[2]["messages"][0]["content"]
    assert "Today" in client.requests[0]["messages"][0]["content"] or "TODAY" in client.requests[0]["messages"][0]["content"]
    stored = conn.execute("SELECT note, note_status FROM people").fetchone()
    assert stored["note_status"] == "checked" and "not usable" not in stored["note"]


def test_note_that_still_fails_is_marked_not_usable(conn, config, paths, master):
    from applier.network import draft_note
    from applier.resume.model import save_master
    from tests.test_tailor import FakeClaude

    save_master(paths.master_file, master)
    company = make_company(conn, "Bright Labs")
    with conn:
        conn.execute("INSERT INTO people (company_id, name, title, source_url, why) VALUES (?, 'Dana Reyes', 'CEO', "
                     "'https://brightlabs.example/team', 'runs the company')", (company,))
    person = conn.execute("SELECT * FROM people").fetchone()
    bad = {"items": [{"id": "n0", "supported": False, "problem": "not supported"}]}
    client = FakeClaude({"body": "i led a team of 50. Could we talk?", "cited_ids": []}, bad,
                        {"body": "i led a team of 50 people. Could we talk?", "cited_ids": []}, bad)
    result = draft_note(client, conn, config, paths, person)
    assert result.status == "failed_check" and result.problems == ["not supported"]
    assert "not usable" in conn.execute("SELECT note FROM people").fetchone()[0]


def test_notes_know_whether_you_applied(conn):
    from applier.network import application_fact

    company = make_company(conn, "Bright Labs")
    assert "has not applied" in application_fact(conn, company)
    app_id = db.create_application(conn, company, "Analyst", status="queued")
    assert "not submitted" in application_fact(conn, company)
    db.mark_submitted(conn, app_id, "submitted", db.utc_now())
    assert application_fact(conn, company).startswith("He applied for the Analyst role")


def test_dashboard_explains_instead_of_opening_when_submitting_is_closed(conn, config, paths, monkeypatch):
    from applier.dashboard import create_app

    started = []
    monkeypatch.setattr("subprocess.Popen", lambda *a, **k: started.append(a))
    company = make_company(conn, "Bright Labs")
    app_id = db.create_application(conn, company, "Analyst", ats="ashby", status="prepared")
    app = create_app(paths)
    client = app.test_client()
    client.post("/action/assist", data={"id": app_id, "token": app.config["ACTION_TOKEN"]},
                environ_base={"REMOTE_ADDR": "127.0.0.1"})
    page = client.get("/review", environ_base={"REMOTE_ADDR": "127.0.0.1"}).get_data(as_text=True)
    assert started == [] and "Not opened" in page and "Nothing was sent" in page


def test_dashboard_buttons_keep_working_after_a_restart(paths):
    from applier.dashboard import create_app

    first, second = create_app(paths), create_app(paths)
    assert first.config["ACTION_TOKEN"] == second.config["ACTION_TOKEN"]
    page = second.test_client().post("/action/assist", data={"id": 1, "token": "old"},
                                     environ_base={"REMOTE_ADDR": "127.0.0.1"})
    assert page.status_code == 403 and "out of date" in page.get_data(as_text=True)
