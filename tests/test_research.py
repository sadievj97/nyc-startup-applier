"""Company facts and funding proposals through web search, tested with a stand-in for the Claude API."""

import json
from datetime import date, timedelta
from types import SimpleNamespace as NS

import pytest

from applier import db
from applier.config import load_targets
from applier.jobs import research
from applier.jobs.ats import Posting
from applier.jobs.scoring import score_posting
from applier.jobs.store import score_all, upsert_postings
from tests.conftest import make_company


def usage(searches=2):
    return NS(input_tokens=20000, output_tokens=500, cache_read_input_tokens=0, cache_creation_input_tokens=0,
              server_tool_use=NS(web_search_requests=searches))


def search_block(*urls):
    return NS(type="web_search_tool_result", content=[NS(url=u, title="t") for u in urls])


def text_block(text):
    return NS(type="text", text=text, citations=None)


class FakeClaude:
    """Returns prepared responses in order and records every request."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []
        self.beta = NS(messages=NS(create=self._create))

    def _create(self, **kwargs):
        self.requests.append(kwargs)
        return self.responses.pop(0)


def response(content, stop="end_turn", model="claude-opus-5-5"):
    return NS(content=content, stop_reason=stop, usage=usage(), model=model)


FACTS = {
    "website": "https://www.testco.com",
    "founded_year": 2019, "founded_source": "https://testco.com/about",
    "headcount": 120, "headcount_source": "https://news.example.com/testco-raises",
    "is_private": True, "private_source": "https://news.example.com/testco-raises",
    "industry": "fintech", "industry_source": "https://www.unverified.example.org/page",
    "uncertain": [],
}


@pytest.fixture
def company(conn):
    company_id = make_company(conn, "Testco", website="https://testco.com")
    return conn.execute("SELECT * FROM companies WHERE id = ?", (company_id,)).fetchone()


def test_official_website_is_saved_only_if_the_search_saw_it(conn, config):
    company_id = make_company(conn, "Nosite Inc")
    row = conn.execute("SELECT * FROM companies WHERE id = ?", (company_id,)).fetchone()
    facts = dict(FACTS, website="https://nosite.example")
    claude = FakeClaude(response([search_block("https://nosite.example/about"), text_block(json.dumps(facts))]))
    research.research_company(claude, conn, config, row)
    assert conn.execute("SELECT website FROM companies WHERE id = ?", (company_id,)).fetchone()[0] == "https://nosite.example"

    class Site:  # a homepage the search did not show, visited directly
        def __init__(self, page):
            self.page = page

        def get_text(self, url):
            return self.page

    company_id = make_company(conn, "Visited Co")
    row = conn.execute("SELECT * FROM companies WHERE id = ?", (company_id,)).fetchone()
    claude = FakeClaude(response([search_block("https://news.example.com/x"),
                                  text_block(json.dumps(dict(FACTS, website="https://visited.example")))]))
    research.research_company(claude, conn, config, row, http=Site("<h1>Visited Co builds things</h1>"))
    assert conn.execute("SELECT website FROM companies WHERE id = ?", (company_id,)).fetchone()[0] == "https://visited.example"

    company_id = make_company(conn, "Other Inc")
    row = conn.execute("SELECT * FROM companies WHERE id = ?", (company_id,)).fetchone()
    facts = dict(FACTS, website="https://made-up.example")
    claude = FakeClaude(response([search_block("https://news.example.com/x"), text_block(json.dumps(facts))]))
    research.research_company(claude, conn, config, row)
    assert conn.execute("SELECT website FROM companies WHERE id = ?", (company_id,)).fetchone()[0] is None


def test_facts_are_saved_and_unverified_sources_are_not_trusted(conn, config, company):
    claude = FakeClaude(response([
        search_block("https://testco.com/about/", "https://news.example.com/testco-raises"),
        text_block("Here is the answer: " + json.dumps(FACTS)),
    ]))
    outcome = research.research_company(claude, conn, config, company)

    assert outcome.facts.founded_year == 2019
    assert outcome.uncertain == ["industry"]  # its source never appeared in the search results
    facts = research.load_facts(conn, company["id"])
    assert facts["founded_year"] == 2019 and facts["industry_source"] is None
    assert facts["uncertain"] == ["industry"]
    assert conn.execute("SELECT industry FROM companies WHERE id = ?", (company["id"],)).fetchone()[0] is None
    flags = [r["message"] for r in conn.execute("SELECT message FROM flags WHERE kind = 'company_facts_uncertain'")]
    assert flags and "industry" in flags[0]


def test_only_the_web_search_tool_is_given(conn, config, company):
    claude = FakeClaude(response([search_block("https://testco.com/about"), text_block(json.dumps(FACTS))]))
    research.research_company(claude, conn, config, company)
    request = claude.requests[0]
    assert [t["type"] for t in request["tools"]] == ["web_search_20260209"]  # rule 7
    assert request["model"] == config.models.research
    assert request["fallbacks"] == "default" and request["betas"] == ["server-side-fallback-2026-07-01"]


def test_usage_and_cost_are_logged(conn, config, company):
    claude = FakeClaude(response([search_block("https://testco.com/about"), text_block(json.dumps(FACTS))]))
    outcome = research.research_company(claude, conn, config, company)
    row = conn.execute("SELECT * FROM api_usage").fetchone()
    assert (row["input_tokens"], row["output_tokens"], row["web_searches"]) == (20000, 500, 2)
    expected = (20000 * 4 + 500 * 20) / 1_000_000 + 2 * 0.01
    assert row["cost_usd"] == pytest.approx(expected) and outcome.cost_usd == pytest.approx(expected)


def test_paused_turn_is_continued(conn, config, company):
    claude = FakeClaude(
        response([search_block("https://testco.com/about")], stop="pause_turn"),
        response([text_block(json.dumps(FACTS))]),
    )
    outcome = research.research_company(claude, conn, config, company)
    assert outcome.facts is not None and len(claude.requests) == 2
    assert claude.requests[1]["messages"][1]["role"] == "assistant"


def test_bad_answers_and_refusals_keep_the_company_and_flag_it(conn, config, company):
    claude = FakeClaude(response([text_block("I could not find anything.")]))
    outcome = research.research_company(claude, conn, config, company)
    assert outcome.facts is None and "not valid JSON" in outcome.error
    claude = FakeClaude(response([text_block("")], stop="refusal"))
    outcome = research.research_company(claude, conn, config, company)
    assert outcome.error == "the request was declined"
    assert conn.execute("SELECT COUNT(*) FROM flags WHERE kind = 'company_facts_uncertain'").fetchone()[0] == 2


def test_budget_stops_research(conn, config, company):
    conn.execute("INSERT INTO api_usage (model, purpose, cost_usd) VALUES ('m', 'x', ?)", (config.daily_budget_usd,))
    conn.commit()
    with pytest.raises(research.BudgetReached):
        research.research_company(FakeClaude(), conn, config, company)


def test_startup_filter_uses_confirmed_facts_only(paths, config):
    targets = load_targets(paths.targets_file)
    posting = Posting("ashby", "1", "Chief of Staff", location="New York, NY", workplace_type="onsite")
    data = dict(posting.__dict__, other_locations="[]")

    big = {"founded_year": 2005, "headcount": 4000, "is_private": False, "uncertain": []}
    result = score_posting(data, {"facts": big}, targets, config)
    assert result.decision == "filtered_startup"
    assert any("public company" in f for f in result.filters)

    unsure = {"founded_year": 2005, "headcount": 4000, "is_private": False,
              "uncertain": ["founded_year", "headcount", "is_private"]}
    result = score_posting(data, {"facts": unsure}, targets, config)
    assert result.decision == "eligible"  # unclear facts keep the company in (brief 3)
    assert "not confirmed" in result.explanation()

    assert "Startup facts not checked yet" in score_posting(data, {}, targets, config).explanation()


def test_score_all_reads_saved_facts(conn, paths, config, company):
    targets = load_targets(paths.targets_file)
    facts = research.CompanyFacts(founded_year=2010, founded_source="https://testco.com/about", headcount=50,
                                  headcount_source="https://testco.com/about", is_private=True,
                                  private_source="https://testco.com/about", industry="fintech",
                                  industry_source="https://testco.com/about")
    research.save_facts(conn, company["id"], facts, research.check_sources(facts, {"testco.com/about"}))
    upsert_postings(conn, company["id"], "ashby",
                    [Posting("ashby", "1", "Chief of Staff", location="New York, NY", workplace_type="onsite")],
                    db.utc_now())
    score_all(conn, targets, config)
    row = conn.execute("SELECT decision, decision_reason FROM postings").fetchone()
    assert row["decision"] == "filtered_startup" and "founded 2010" in row["decision_reason"]


def test_funding_news_becomes_proposals_only(conn, config, paths):
    from applier.companies import load_aliases

    found = {"startups": [
        {"name": "Fresh AI", "website": "https://fresh.ai", "round": "Seed", "amount": "$5M",
         "announced": "2026-10-01", "source_url": "https://news.example.com/fresh"},
        {"name": "Ghost Co", "website": None, "round": "A", "amount": "$20M", "announced": None,
         "source_url": "https://made-up.example.com/ghost"},
        {"name": "Northwind", "website": None, "round": "C", "amount": "$100M", "announced": None,
         "source_url": "https://news.example.com/northwind"},
    ]}
    from applier.blocklist import sync_company_blocks

    sync_company_blocks(conn, paths, config)
    claude = FakeClaude(response([search_block("https://news.example.com/fresh", "https://news.example.com/northwind"),
                                  text_block(json.dumps(found))]))
    outcome = research.propose_funded(claude, conn, config, load_aliases(paths.aliases_csv),
                                      today=date(2026, 10, 12))
    assert [p.name for p in outcome.proposals] == ["Fresh AI"]
    skipped = dict(outcome.skipped)
    assert "not among the search results" in skipped["Ghost Co"]
    assert skipped["Northwind"] == "on your never-apply lists"
    row = conn.execute("SELECT discovery_status, discovery_source FROM companies WHERE name = 'Fresh AI'").fetchone()
    assert tuple(row) == ("proposed", "funding_news")


def test_proposal_check_keeps_only_sourced_facts(conn, config):
    import json as _json

    from applier.jobs.research import check_proposal
    from tests.conftest import make_company
    from tests.test_network import FakeSearch

    company_id = make_company(conn, "Antioch", discovery_status="proposed", discovery_source="funding_news")
    company = conn.execute("SELECT * FROM companies WHERE id = ?", (company_id,)).fetchone()
    answer = {"website": "https://antioch.example", "description": "Builds software for labs.",
              "description_source": "https://antioch.example/about", "nyc_based": True,
              "nyc_source": "https://unseen.example/story", "location": "New York, NY", "founded_year": 2022,
              "founded_source": "https://antioch.example/about", "headcount": 40, "headcount_source": None,
              "is_private": True, "private_source": "https://antioch.example/about", "industry": "lab software",
              "industry_source": "https://antioch.example/about", "funding": "Series A, $32M",
              "funding_source": "https://news.example/antioch-series-a", "uncertain": []}
    client = FakeSearch(["https://antioch.example/about", "https://news.example/antioch-series-a"], answer)
    outcome = check_proposal(client, conn, config, company)
    assert outcome.confirmed == {"description": True, "nyc_based": False, "funding": True}  # NYC source not seen
    facts = {r["fact"]: r for r in conn.execute("SELECT * FROM company_facts WHERE company_id = ?", (company_id,))}
    assert _json.loads(facts["description"]["value"]) == "Builds software for labs."
    assert facts["nyc_based"]["source_url"] is None and facts["nyc_based"]["uncertain"] == 1
    assert conn.execute("SELECT discovery_status FROM companies WHERE id = ?", (company_id,)).fetchone()[0] == "proposed"
