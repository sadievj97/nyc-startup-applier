"""Rule 2: answers the user owns come only from profile.yaml, salary by a fixed rule."""

import pytest
import yaml

from applier.config import ConfigError
from applier.paths import PROJECT_ROOT
from applier.profile import load_profile, salary_answer
from tests.conftest import FIXTURES


@pytest.fixture
def profile():
    return load_profile(FIXTURES / "profile.yaml")


def test_fixture_profile_is_valid(profile):
    assert profile.needs_sponsorship
    assert profile.work_authorization.authorized_to_work_in_us
    assert profile.eeo.gender == "Decline to self-identify"


@pytest.mark.skipif(not (PROJECT_ROOT / "profile" / "profile.yaml").exists(), reason="no personal profile yet")
def test_your_profile_is_valid():
    load_profile(PROJECT_ROOT / "profile" / "profile.yaml")


def test_inconsistent_sponsorship_answers_are_rejected(tmp_path):
    data = yaml.safe_load((FIXTURES / "profile.yaml").read_text(encoding="utf-8"))
    data["sponsorship"]["requires_now_or_in_future"] = False
    path = tmp_path / "profile.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    with pytest.raises(ConfigError, match="sponsorship"):
        load_profile(path)


def test_salary_is_the_midpoint_of_the_posted_range(profile):
    answer = salary_answer(profile.salary, 120000, 160000)
    assert answer.number == 140000 and answer.text == "$140,000" and answer.needs_me is None

    uneven = salary_answer(profile.salary, 130000, 145000)
    assert uneven.number == 137500 and uneven.text == "$137,500"


def test_reversed_range_is_handled(profile):
    assert salary_answer(profile.salary, 160000, 120000).number == 140000


def test_no_posted_range_uses_expected_range(profile):
    answer = salary_answer(profile.salary, None, None)
    assert answer.number == 145000
    assert answer.text == "$140,000 to $150,000"


@pytest.mark.parametrize(
    "low, high, currency, interval, reason",
    [
        (100000, 140000, "USD", "year", "below your $130,000 floor"),
        (60, 80, "USD", "hour", "per hour"),
        (120000, 160000, "EUR", "year", "EUR"),
        (150000, None, "USD", "year", "only one end"),
    ],
)
def test_cases_the_rule_does_not_cover_go_to_you(profile, low, high, currency, interval, reason):
    answer = salary_answer(profile.salary, low, high, currency, interval)
    assert answer.number is None and answer.text is None
    assert reason in answer.needs_me


def test_salary_text_has_no_dashes(profile):
    for answer in (salary_answer(profile.salary, None, None), salary_answer(profile.salary, 140000, 160000)):
        assert "—" not in answer.text and "–" not in answer.text and "-" not in answer.text
