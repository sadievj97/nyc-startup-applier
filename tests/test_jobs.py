"""Phase 3: hiring-system readers, detection, filters, scoring, storage and one-per-company choice."""

import json
from datetime import datetime, timedelta, timezone

import pytest

from applier import db
from applier.blocklist import sync_company_blocks
from applier.config import load_targets
from applier.jobs.ats import Posting, apply_greenhouse_details, parse_ashby, parse_greenhouse, parse_lever
from applier.jobs.detect import _mentions_company, boards_in_text, is_blocked_site, slug_candidates
from applier.jobs.hn import parse_post
from applier.jobs.rules import ai_policy_note, location_verdict, not_full_time, required_years, sponsorship_stance
from applier.jobs.scoring import has_term, score_posting
from applier.jobs.store import choose_per_company, score_all, upsert_postings
from applier.jobs.text import html_to_text
from tests.conftest import FIXTURES, make_company

NOW = datetime(2026, 10, 12, 16, 0, tzinfo=timezone.utc)


def load(name):
    return json.loads((FIXTURES / "ats" / name).read_text(encoding="utf-8"))


@pytest.fixture
def targets(paths):
    return load_targets(paths.targets_file)


# ---------------------------------------------------------------- readers


def test_ashby_reader():
    postings = {p.ats_posting_id: p for p in parse_ashby(load("ashby.json"), "testco")}
    assert set(postings) == {"a1", "a3"}  # the unlisted job is skipped (brief 3)
    first = postings["a1"]
    assert (first.title, first.workplace_type, first.employment_type) == ("Founding Strategy & Ops", "onsite", "FullTime")
    assert (first.pay_min, first.pay_max, first.pay_currency, first.pay_interval) == (120000, 160000, "USD", "year")
    assert first.pay_raw == "$120K - $160K"
    assert "New York, NY" in first.other_locations
    assert postings["a3"].workplace_type == "remote"


def test_greenhouse_reader_and_details():
    [posting] = parse_greenhouse(load("greenhouse.json"), "testco")
    assert posting.workplace_type == "hybrid"
    assert posting.location == "New York, NY"
    assert "unable to sponsor" in posting.description_text and "&lt;" not in posting.description_text
    apply_greenhouse_details(posting, load("greenhouse_job.json"))
    assert (posting.pay_min, posting.pay_max) == (140000, 170000)  # the New York range, not the remote one
    assert posting.extra["include_ai_disclaimer"] is True
    assert posting.published_at.startswith("2026-09-30")
    assert posting.extra["questions"][0]["label"] == "Resume/CV"


def test_lever_reader():
    postings = {p.ats_posting_id: p for p in parse_lever(load("lever.json"), "testco")}
    first = postings["l1"]
    assert (first.workplace_type, first.employment_type, first.pay_interval) == ("hybrid", "Full-time", "year")
    assert first.other_locations == ["Boston, MA"]
    assert "3-5 years of experience" in first.description_text
    assert "do not use AI" in first.description_text
    assert first.published_at.startswith("2026-")


def test_html_to_text():
    assert html_to_text("<p>One</p><ul><li>Two</li></ul><script>x()</script>") == "One\n\n- Two"
    assert html_to_text("&lt;p&gt;Encoded &amp;amp; fine&lt;/p&gt;") == "Encoded & fine"


# ---------------------------------------------------------------- detection


@pytest.mark.parametrize("text, expected", [
    ('<a href="https://jobs.ashbyhq.com/acme">Jobs</a>', ("ashby", "acme")),
    ("https://job-boards.greenhouse.io/acme/jobs/123", ("greenhouse", "acme")),
    ("https://boards.greenhouse.io/embed/job_board?for=acme&b=x", ("greenhouse", "acme")),
    ("https://jobs.lever.co/acme/abc-123", ("lever", "acme")),
    ("https://apply.workable.com/acme/", ("workable", "acme")),
])
def test_board_links_are_found(text, expected):
    ref = boards_in_text(text)[0]
    assert (ref.ats, ref.token) == expected


def test_job_boards_and_networks_are_never_opened():
    for url in ["https://www.linkedin.com/company/koop", "https://wellfound.com/company/x",
                "https://builtinnyc.com/job/1", "https://www.indeed.com/viewjob"]:
        assert is_blocked_site(url)
    assert not is_blocked_site("https://koop.ai/careers")
    assert slug_candidates("Koop AI", "https://www.linkedin.com/company/koop") == ["koopai", "koop-ai", "koop"]


def test_guessed_board_must_mention_the_company():
    linkedin = [Posting("lever", "1", "Engineer", description_text="Join LinkedIn to connect the world.")]
    assert not _mentions_company(linkedin, "Koop AI")
    spade = [Posting("ashby", "1", "Analyst", description_text="At Spade, we enrich card data.")]
    assert _mentions_company(spade, "Spade")


# ---------------------------------------------------------------- rules


@pytest.mark.parametrize("location, others, workplace, remote_ok, ok", [
    ("New York City", [], "onsite", False, True),
    ("New York, NY", [], "hybrid", False, True),
    ("San Francisco", ["New York, NY"], "unknown", False, True),
    ("Remote (US)", ["New York City"], "remote", False, False),
    ("Remote (US)", [], "remote", True, True),
    ("Boston, MA", [], "onsite", False, False),
    ("Brooklyn, NY", [], "unknown", False, True),
])
def test_location_rule(location, others, workplace, remote_ok, ok):
    assert location_verdict(location, others, workplace, "Analyst", remote_ok).ok is ok


@pytest.mark.parametrize("text, stance", [
    ("We are unable to offer visa sponsorship for this position.", "no"),
    ("Candidates must be authorized to work in the US without the need for current or future sponsorship.", "no"),
    ("This role requires an active security clearance.", "no"),
    ("Visa sponsorship is available for this role.", "yes"),
    ("We value ownership and speed.", None),
])
def test_sponsorship_rule(text, stance):
    assert sponsorship_stance(text)[0] == stance


def test_everify_is_noted(targets, config):
    posting = Posting("ashby", "e", "Chief of Staff", location="New York, NY", workplace_type="onsite",
                      description_text="Great role. Testco participates in E-Verify.")
    result = score_posting(posting_dict(posting), {}, targets, config)
    assert "E-Verify: posting says" in result.explanation()


def test_ai_policy_rule():
    assert ai_policy_note("Please do not use AI tools to write your application answers.")
    assert ai_policy_note("AI-generated applications will be rejected.")
    assert ai_policy_note("We build AI tools for lawyers.") is None


@pytest.mark.parametrize("text, years", [
    ("3-5 years of experience in operations", 3),
    ("5+ years in strategy or consulting; 2+ years of relevant experience", 5),
    ("We have 10 years of data and serve 3 million users", None),
    ("at least 4 years of work in finance", 4),
])
def test_years_rule(text, years):
    assert required_years(text) == years


def test_full_time_rule():
    assert not_full_time("Internship", "Operations Intern")
    assert not_full_time("PartTime", "Analyst")
    assert not_full_time("FullTime", "AI Strategy Fellow") is None
    assert not_full_time(None, "International Partnerships Associate") is None


def test_title_matching_reads_ampersands_and_abbreviations():
    assert has_term("Founding Strategy & Ops", "strategy and operations")
    assert has_term("BizOps Associate", "bizops")
    assert has_term("Founder's Associate", "founder's associate")
    assert not has_term("International Relations", "intern")
    assert has_term("Sr. Manager, BizOps", "senior manager")


# ---------------------------------------------------------------- scoring


def posting_dict(posting: Posting) -> dict:
    data = dict(posting.__dict__)
    data["other_locations"] = json.dumps(posting.other_locations)
    return data


def test_a_strong_fit_scores_high_with_reasons(targets, config):
    posting = {p.ats_posting_id: p for p in parse_ashby(load("ashby.json"), "testco")}["a1"]
    result = score_posting(posting_dict(posting), {"industry": "Fintech"}, targets, config)
    assert result.decision == "eligible"
    assert result.score >= 85
    explanation = result.explanation()
    assert "title matches strategy and operations (+40)" in explanation
    assert "preferred industry: Fintech (+10)" in explanation


def test_filters_keep_the_score_and_the_evidence(targets, config):
    [posting] = parse_greenhouse(load("greenhouse.json"), "testco")
    result = score_posting(posting_dict(posting), {}, targets, config)
    assert result.decision == "filtered_sponsorship"
    assert "unable to sponsor" in result.sponsorship_note
    assert result.score > 0


def test_too_senior_and_excluded_industry(targets, config):
    senior = Posting("ashby", "x", "Director of Strategy & Operations", location="New York, NY", workplace_type="onsite")
    assert score_posting(posting_dict(senior), {}, targets, config).decision == "filtered_role"
    chief = Posting("ashby", "y", "Chief of Staff", location="New York, NY", workplace_type="onsite")
    assert score_posting(posting_dict(chief), {}, targets, config).decision == "eligible"
    assert score_posting(posting_dict(chief), {"industry": "EdTech for schools"}, targets, config).decision == "filtered_industry"
    # Construction tech is allowed; plain construction is not.
    assert score_posting(posting_dict(chief), {"industry": "construction tech AI"}, targets, config).decision == "eligible"
    assert score_posting(posting_dict(chief), {"industry": "commercial construction"}, targets, config).decision == "filtered_industry"


def test_ai_request_is_noted(targets, config):
    posting = {p.ats_posting_id: p for p in parse_lever(load("lever.json"), "testco")}["l1"]
    result = score_posting(posting_dict(posting), {}, targets, config)
    assert result.ai_policy_note and "will not be auto-submitted" in result.explanation()


# ---------------------------------------------------------------- storage and choice


def test_new_updated_and_closed_postings(conn):
    company = make_company(conn, "Testco")
    first = parse_ashby(load("ashby.json"), "testco")
    counts = upsert_postings(conn, company, "ashby", first, NOW)
    assert (counts.new, counts.updated, counts.closed) == (2, 0, 0)

    later = NOW + timedelta(days=1)
    counts = upsert_postings(conn, company, "ashby", first[:1], later)
    assert (counts.new, counts.updated, counts.closed) == (0, 1, 1)
    rows = {r["ats_posting_id"]: r for r in conn.execute("SELECT * FROM postings")}
    assert rows["a1"]["first_seen"] == db.to_db_time(NOW)  # first seen is kept
    assert rows["a1"]["last_seen"] == db.to_db_time(later)
    assert rows["a3"]["decision"] == "closed"


def test_one_posting_per_company(conn, targets, config):
    company = make_company(conn, "Testco")
    cos = Posting("ashby", "c1", "Chief of Staff", location="New York, NY", workplace_type="onsite",
                  description_text="Excel, pricing, strategy, operations, stakeholder, budget.")
    partner = Posting("ashby", "c2", "Partnerships Associate", location="New York, NY", workplace_type="onsite",
                      description_text="Excel, pricing, strategy, operations.")
    weak = Posting("ashby", "c3", "Data Analyst", location="New York, NY", workplace_type="onsite")
    upsert_postings(conn, company, "ashby", [cos, partner, weak], NOW)
    score_all(conn, targets, config)
    choose_per_company(conn, config)
    decisions = {r["ats_posting_id"]: r["decision"] for r in conn.execute("SELECT * FROM postings")}
    # Partnerships Associate (80: associate level fits) beats Chief of Staff (75: no level in title).
    assert decisions == {"c1": "skipped_one_per_company", "c2": "chosen", "c3": "below_min_score"}


def test_blocked_and_applied_companies_get_no_choice(conn, paths, config, targets):
    sync_company_blocks(conn, paths, config)
    blocked = make_company(conn, "Northwind")
    applied = make_company(conn, "Applied Co")
    db.create_application(conn, applied, "Associate")
    role = dict(title="Chief of Staff", location="New York, NY", workplace_type="onsite")
    upsert_postings(conn, blocked, "ashby", [Posting("ashby", "m1", **role)], NOW)
    upsert_postings(conn, applied, "ashby", [Posting("ashby", "p1", **role)], NOW)
    score_all(conn, targets, config)
    choose_per_company(conn, config)
    rows = list(conn.execute("SELECT decision, decision_reason FROM postings ORDER BY ats_posting_id"))
    assert [r["decision"] for r in rows] == ["blocked_company", "blocked_company"]
    assert "never apply" in rows[0]["decision_reason"] and "already applied" in rows[1]["decision_reason"]


# ---------------------------------------------------------------- Hacker News


def test_hn_post_parsing():
    item = {"id": 1, "text": 'Acme Robotics (YC W24) | Chief of Staff | New York, NY | ONSITE<p>We build robots. '
                             '<a href="https:&#x2F;&#x2F;www.linkedin.com&#x2F;company&#x2F;acme">LinkedIn</a> '
                             '<a href="https:&#x2F;&#x2F;acme.dev">acme.dev</a> '
                             '<a href="https:&#x2F;&#x2F;jobs.ashbyhq.com&#x2F;acme">Jobs</a>'}
    post = parse_post(item)
    assert post.company == "Acme Robotics"
    assert post.website == "https://acme.dev"  # not LinkedIn, not the job board
    assert (post.boards[0].ats, post.boards[0].token) == ("ashby", "acme")
    assert parse_post({"id": 2, "text": "Acme | Engineer | San Francisco, CA | ONSITE<p>SF only."}) is None
