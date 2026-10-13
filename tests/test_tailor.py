"""Phase 4: tailoring, the fact-check gate, cover letters and packets (with a stand-in for the Claude API)."""

import json
from pathlib import Path
from types import SimpleNamespace as NS

import pytest
import yaml

from applier import db
from applier.resume.model import save_master
from applier.tailor import cover, factcheck, select
from applier.tailor.packet import build_packet, packet_dir, packet_passed, recheck_packet
from tests.conftest import FIXTURES, make_company
from tests.test_resume import built, make_docx, spec, FAKE_A, FAKE_B

BROWSERS = Path(__file__).resolve().parent.parent / ".playwright-browsers"


class FakeClaude:
    """Answers each request with the next prepared JSON object, and records the requests."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.requests = []
        self.beta = NS(messages=NS(create=self._create))

    def _create(self, **kwargs):
        self.requests.append(kwargs)
        answer = self.answers.pop(0)
        return NS(content=[NS(type="text", text=json.dumps(answer))], stop_reason="end_turn",
                  usage=NS(input_tokens=1000, output_tokens=200, cache_read_input_tokens=0,
                           cache_creation_input_tokens=0, server_tool_use=None),
                  model=kwargs["model"])


@pytest.fixture
def master(tmp_path):
    folder = tmp_path / "source"
    folder.mkdir()
    make_docx(folder / "a.docx", FAKE_A)
    make_docx(folder / "b.docx", FAKE_B)
    groups = spec()
    groups.conflicts[0].resolution = "a"  # "the President's office" is the true version
    result, _ = built(folder, groups)
    result.meta.status = "confirmed"
    result.meta.content_hash = result.content_hash()
    return result


def ids(master):
    out = {}
    for group in master.all_groups():
        for v in group.variants:
            out[v.text] = (group.id, v.id, v.status)
    return out


FORECAST = "Built a forecasting model covering 40 stores that cut stockouts by 12%"
OFFICE = "Met the President's office of Acme to present findings"
PRESIDENT = "Met the President of Acme to present findings"


def selection(master, text=None, title="Analyst"):
    known = ids(master)
    return {
        "roles": [{"role_id": "acme", "title": title, "descriptor": "", "bullets": [
            {"variant_id": known[FORECAST][1], "text": text or FORECAST},
            {"variant_id": known[OFFICE][1], "text": OFFICE},
        ]}],
        "education": [{"education_id": "state", "bullets": [
            {"variant_id": known["GPA 4.0/4.0; Dean's List"][1], "text": "GPA 4.0/4.0; Dean's List"}]}],
        "leadership": [],
        "skills": [{"variant_id": known["Technical: R (advanced); Python (intermediate)"][1],
                    "text": "Technical: R (advanced); Python (intermediate)"}],
        "keywords_mirrored": ["forecasting"],
        "rationale": "fits the analytics focus",
    }


# ---------------------------------------------------------------- selection


def test_catalog_lists_only_confirmed_wordings(master):
    catalog = select.master_catalog(master)
    assert FORECAST in catalog and OFFICE in catalog
    assert PRESIDENT not in catalog  # excluded by your answer
    assert "allowed titles: Analyst" in catalog


def test_answers_outside_the_catalog_are_dropped(master):
    known = ids(master)
    answer = selection(master, title="Head of Analytics")
    answer["roles"][0]["bullets"] += [
        {"variant_id": "pDOESNOT", "text": "Invented"},
        {"variant_id": known[PRESIDENT][1], "text": PRESIDENT},
        {"variant_id": known["Built a demand model for 40 stores in Excel"][1], "text": "Built a demand model"},
    ]
    tailored, problems = select.to_tailored(answer, master, {"id": 1})
    texts = [b.text for _, b in tailored.all_bullets()]
    assert PRESIDENT not in texts and "Invented" not in texts
    assert tailored.roles[0].title == "Analyst"  # an unconfirmed title is replaced by your real one
    assert len(problems) == 4  # title, unknown id, excluded wording, same achievement twice


def test_required_roles_are_always_present(master):
    answer = selection(master)
    answer["roles"] = []
    tailored, problems = select.to_tailored(answer, master, {"id": 1})
    assert [r.role_id for r in tailored.roles] == ["acme"]
    assert tailored.roles[0].bullets and "was missing" in problems[0]


# ---------------------------------------------------------------- the gate


def tailored_from(master, **kw):
    tailored, _ = select.to_tailored(selection(master, **kw), master, {"id": 1})
    return tailored


def test_verbatim_resume_passes_without_a_model_call(master, conn, config):
    claude = FakeClaude()
    report = factcheck.run_gate(claude, conn, config, tailored_from(master), master)
    assert report.passed and report.verbatim == 4 and not report.model_ran and not claude.requests


def test_planted_fake_metric_is_blocked(master, conn, config):
    """Acceptance test: the fact-check gate blocks a resume with a planted fake metric."""
    planted = FORECAST + ", growing revenue 47% in six months"
    claude = FakeClaude()
    report = factcheck.run_gate(claude, conn, config, tailored_from(master, text=planted), master)
    assert not report.passed
    assert any("47%" in f.problem and f.by == "code" for f in report.findings)
    assert not claude.requests  # blocked by code; no need to pay for the model check


def test_new_names_tools_and_tampering_are_blocked(master, conn, config):
    sql = tailored_from(master, text="Built a forecasting model in SQL covering 40 stores that cut stockouts by 12%")
    findings, _, _ = factcheck.code_checks(sql, master)
    assert any("SQL" in f.problem for f in findings)

    tampered = tailored_from(master)
    tampered.roles[0].dates = "2019 - Present"
    tampered.roles[0].title = "Senior Analyst"
    tampered.roles[0].bullets[0].source_text = "Built a forecasting model covering 400 stores"
    problems = [f.problem for f in factcheck.code_checks(tampered, master)[0]]
    assert "dates does not match the master resume" in problems
    assert "title does not match the master resume" in problems
    assert "the recorded source wording was changed" in problems

    styled = tailored_from(master, text="Built a forecasting model covering 40 stores — it didn't stop")
    assert any("em dash" in f.problem for f in factcheck.code_checks(styled, master)[0])


def test_rewording_goes_to_the_model_and_its_verdict_counts(master, conn, config):
    reworded = "Built a forecasting model for 40 stores that reduced stockouts by 12%"
    claude = FakeClaude({"items": [{"id": "r1", "supported": True, "problem": ""}]})
    report = factcheck.run_gate(claude, conn, config, tailored_from(master, text=reworded), master)
    assert report.passed and report.reworded == 1 and report.model_ran
    request = claude.requests[0]
    assert request["model"] == config.models.fact_check and "tools" not in request
    assert FORECAST in request["messages"][0]["content"]  # the source was shown to the checker

    claude = FakeClaude({"items": [{"id": "r1", "supported": False, "problem": "'reduced' is stronger than 'cut'"}]})
    report = factcheck.run_gate(claude, conn, config, tailored_from(master, text=reworded), master)
    assert not report.passed and report.findings[0].by == "model"

    claude = FakeClaude({"items": []})  # a missing verdict blocks too
    assert not factcheck.run_gate(claude, conn, config, tailored_from(master, text=reworded), master).passed


@pytest.mark.parametrize("text, expected", [
    ("cut costs by $1.7M across 3 provinces", {"1.7m", "3"}),
    ("4,471 units and ~380 students", {"4471", "380"}),
    ("two government missions", {"2"}),
    ("grew 12% in 2025", {"12%", "2025"}),
])
def test_numbers(text, expected):
    assert factcheck.numbers(text) == expected


def test_abbreviation_of_a_spelled_out_term_is_allowed(master):
    tailored = tailored_from(master)
    group = next(g for g in master.all_groups() if g.id == "acme_model")
    variant = next(v for v in group.variants if v.text == FORECAST)
    variant.text = FORECAST.replace("model", "go-to-market model")
    bullet = tailored.roles[0].bullets[0]
    bullet.source_text = variant.text
    bullet.text = variant.text.replace("go-to-market", "GTM")
    assert not factcheck.code_checks(tailored, master)[0]


# ---------------------------------------------------------------- cover letters


def test_cover_letter_citations_are_checked(master, conn, config):
    from applier.profile import load_profile

    profile = load_profile(FIXTURES / "profile.yaml")
    known = ids(master)
    pages = [cover.Page("https://acme.example", "Acme builds forecasting tools for retailers.")]
    answer = {
        "paragraphs": ["Dear Hiring Team,", "I built a forecasting model covering 40 stores.",
                       "Acme builds forecasting tools for retailers.", "Sincerely,\nTest Person"],
        "applicant_sources": [known[FORECAST][1], "pFAKE123"],
        "company_facts": [{"fact": "builds forecasting tools", "url": "https://acme.example"},
                          {"fact": "raised $50M", "url": "https://news.example/acme"}],
    }
    letter = cover.write_cover_letter(FakeClaude(answer), conn, config, master, profile, "", {"title": "Analyst"},
                                      "Acme", pages)
    assert letter.applicant_texts == [FORECAST]
    assert any("pFAKE123" in p for p in letter.problems)
    assert any("news.example" in p for p in letter.problems)
    style = '# Style\n## Approved self-descriptions\n- "a short approved phrase" (note)\n\n## Other\n- "not this one"\n'
    assert cover.approved_phrases(style) == ["a short approved phrase"]
    context = cover.letter_context(master, style, {"title": "Analyst", "description_text": "Posting text."},
                                   letter.applicant_sources)
    items = cover.cover_check_items(letter, profile, context)
    sources = " | ".join(items[1].sources)
    assert len(items) >= 4
    assert f"Confirmed wording (role/acme): {FORECAST}" in sources
    assert "Built a demand model for 40 stores in Excel" in sources  # same role
    assert f"(role/acme): {OFFICE}" in sources and PRESIDENT not in sources  # never an excluded wording
    assert "Role: Acme Labs" in sources and "Education: State University" in sources
    assert "a short approved phrase" in sources and "Posting text." in sources


def test_shared_sources_are_listed_once(conn, config):
    shared = ["source one", "source two"]
    items = [factcheck.CheckItem(f"c{n}", "cover letter", f"Sentence {n}.", shared) for n in range(3)]
    claude = FakeClaude({"items": [{"id": f"c{n}", "supported": True, "problem": ""} for n in range(3)]})
    findings, _ = factcheck.model_check(claude, conn, config, items)
    prompt = claude.requests[0]["messages"][0]["content"]
    assert not findings and prompt.count("source one") == 1 and prompt.count("source set S1") == 3


# ---------------------------------------------------------------- packets


@pytest.fixture
def posting(conn, paths, master):
    save_master(paths.master_file, master)
    company = make_company(conn, "Acme Labs")
    with conn:
        cur = conn.execute(
            """INSERT INTO postings (company_id, ats, ats_posting_id, title, location, description_text, url)
               VALUES (?, 'ashby', 'x1', 'Data Analyst', 'New York, NY', 'Forecasting and analytics role.', 'https://x')""",
            (company,))
    return cur.lastrowid


@pytest.mark.skipif(not BROWSERS.exists(), reason="Playwright browser not installed")
def test_packet_end_to_end(conn, config, paths, posting, master, monkeypatch):
    monkeypatch.setattr("applier.paths.Paths.browsers_dir", property(lambda self: BROWSERS))
    result = build_packet(FakeClaude(selection(master)), conn, config, paths, posting)
    assert result.report.passed and result.pages == 1
    assert result.resume.name == "Test_Person_Resume_Acme_Labs.pdf" and result.resume.exists()
    directory = packet_dir(paths, "Acme Labs", posting)
    assert packet_passed(directory)

    # You edit a bullet to plant a fake metric: the packet no longer counts as checked...
    data = yaml.safe_load((directory / "tailored.yaml").read_text(encoding="utf-8"))
    data["roles"][0]["bullets"][0]["text"] += ", which tripled revenue"
    (directory / "tailored.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")
    assert not packet_passed(directory)
    # ...and re-checking blocks it and raises a flag.
    result = recheck_packet(FakeClaude({"items": [{"id": "r1", "supported": False, "problem": "tripled revenue is not in the source"}]}),
                            conn, config, paths, posting)
    assert not result.report.passed and not packet_passed(directory)
    assert conn.execute("SELECT COUNT(*) FROM flags WHERE kind = 'fact_check_failed'").fetchone()[0] == 1


def letter_answer(master, *middle):
    return {"paragraphs": ["Dear Hiring Team,", *middle, "Sincerely,\nTest Person"],
            "applicant_sources": [ids(master)[FORECAST][1]], "company_facts": []}


def verdicts(count, bad=()):
    return {"items": [{"id": f"c{n}", "supported": n not in bad, "problem": "not in the sources" if n in bad else ""}
                      for n in range(count)]}


@pytest.mark.skipif(not BROWSERS.exists(), reason="Playwright browser not installed")
def test_flagged_cover_letter_gets_one_revision(conn, config, paths, posting, master, monkeypatch):
    monkeypatch.setattr("applier.paths.Paths.browsers_dir", property(lambda self: BROWSERS))
    claude = FakeClaude(
        selection(master),
        letter_answer(master, "I built a forecasting model covering 40 stores.", "I also ran sales enablement."),
        verdicts(5, bad={2}),  # "I also ran sales enablement." is not supported
        letter_answer(master, "I built a forecasting model covering 40 stores."),
        verdicts(4),
    )
    result = build_packet(claude, conn, config, paths, posting, cover_letter=True)
    assert result.report.passed and "sales enablement" not in result.cover_letter.text
    assert any("revised once" in note for note in result.notes)
    assert "THE FACT CHECKER FLAGGED" in claude.requests[3]["messages"][0]["content"]
    assert conn.execute("SELECT COUNT(*) FROM flags WHERE kind = 'fact_check_failed'").fetchone()[0] == 0


@pytest.mark.skipif(not BROWSERS.exists(), reason="Playwright browser not installed")
def test_packet_with_planted_metric_is_blocked(conn, config, paths, posting, master, monkeypatch):
    monkeypatch.setattr("applier.paths.Paths.browsers_dir", property(lambda self: BROWSERS))
    result = build_packet(FakeClaude(selection(master)), conn, config, paths, posting, plant=True)
    assert not result.report.passed
    assert any("47%" in f.problem for f in result.report.findings)
