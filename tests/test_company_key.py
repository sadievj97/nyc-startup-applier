import pytest

from applier.companies import company_key, folder_company_name


@pytest.mark.parametrize(
    "variants",
    [
        ["Spark AI", "SparkAI", "Spark AI, Inc.", "spark ai inc", "Spark AI LLC"],
        ["The Bridgespan Group", "Bridgespan Group"],
        ["Moody's", "Moodys", "Moody’s"],
        ["EY Parthenon", "EY-Parthenon", "ey parthenon"],
        ["Strategy&", "Strategy and"],
        ["Clay", "Clay.com", "Clay Inc"],
        ["Café Labs", "Cafe Labs"],
        ["Bluepeak", "Bluepeak AI", "BluepeakAI"],
        ["J.P. Morgan", "JP Morgan"],
        ["Bain & Company", "Bain and Company", "Bain"],
        ["McKinsey & Co.", "McKinsey & Company", "McKinsey"],
        ["Charles River Associates (CRA)", "Charles River Associates"],
    ],
)
def test_spellings_of_one_company_share_a_key(variants):
    keys = {company_key(name) for name in variants}
    assert len(keys) == 1, keys


def test_short_names_are_not_cut_down():
    assert company_key("Kai") == "kai"
    assert company_key("xAI") == "xai"
    assert company_key("Co") == "co"
    assert company_key("A & Co") == "aand"
    assert company_key("(stealth)") == "stealth"


def test_and_inside_a_name_is_kept():
    assert company_key("Strategy&") == "strategyand"
    assert company_key("Alvarez & Marsal") == "alvarezandmarsal"


def test_different_companies_stay_different():
    assert company_key("Ramp") != company_key("Rampart")
    assert company_key("Clay") != company_key("Clayton")


def test_aliases_fix_typos():
    aliases = {company_key("Grand Atlantik"): company_key("Grand Atlantic")}
    assert company_key("Grand Atlantik", aliases) == company_key("Grand Atlantic")


def test_empty_names_are_rejected():
    with pytest.raises(ValueError):
        company_key("   ")
    with pytest.raises(ValueError):
        company_key("!!!")


@pytest.mark.parametrize(
    "folder, expected",
    [
        ("0. Resume", None),
        ("1. Job Search Strategy", None),
        ("Bluepeak - AI", "Bluepeak"),
        ("Edra - Startup", "Edra"),
        ("Alvarez & Marsal", "Alvarez & Marsal"),
        ("Northwind", "Northwind"),
    ],
)
def test_folder_names(folder, expected):
    assert folder_company_name(folder) == expected


def test_folder_suffix_and_spelling_variants_match():
    assert company_key(folder_company_name("SparkAI - Startup")) == company_key("Spark AI")
