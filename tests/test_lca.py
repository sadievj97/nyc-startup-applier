"""Sponsorship history from a (made-up, tiny) Department of Labor LCA file."""

from datetime import datetime

import openpyxl

from applier.jobs import lca

HEADER = ["CASE_NUMBER", "CASE_STATUS", "DECISION_DATE", "VISA_CLASS", "JOB_TITLE", "EMPLOYER_NAME",
          "TRADE_NAME_DBA", "EMPLOYER_POC_EMAIL", "WORKSITE_CITY", "WORKSITE_STATE"]
ROWS = [
    ["I-1", "Certified", datetime(2026, 3, 2), "H-1B", "STRATEGY ASSOCIATE", "Testco, Inc.", None,
     "person@testco.example", "New York", "NY"],
    ["I-2", "Denied", datetime(2026, 5, 20), "H-1B", "DATA ANALYST", "TESTCO INC", "Testco", "a@b.example",
     "Boston", "MA"],
    ["I-3", "Certified", datetime(2026, 1, 9), "E-3", "ANALYST", "Other Labs LLC", None, "x@y.example",
     "Brooklyn", "NY"],
]


def make_file(path):
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.append(HEADER)
    for row in ROWS:
        sheet.append(row)
    workbook.save(path)
    return path


def test_employer_counts(tmp_path):
    summaries, rows = lca.summarize(make_file(tmp_path / "LCA_Disclosure_Data_FY2026_Q3.xlsx"))
    assert rows == 3
    testco = summaries["testco"]
    assert (testco.filings, testco.certified, testco.h1b, testco.nyc) == (2, 1, 2, 1)
    assert testco.latest == "2026-05-20"
    assert summaries["otherlabs"].nyc == 1


def test_no_contact_details_are_stored(tmp_path, conn):
    path = make_file(tmp_path / "LCA_Disclosure_Data_FY2026_Q3.xlsx")
    summaries, rows = lca.summarize(path)
    lca.save(conn, summaries, path, rows)
    stored = " ".join(str(v) for row in conn.execute("SELECT * FROM lca_employers") for v in row)
    assert "@" not in stored
    columns = [c[1] for c in conn.execute("PRAGMA table_info(lca_employers)")]
    assert not any("POC" in c.upper() or "EMAIL" in c.upper() for c in columns)


def test_history_note(tmp_path, conn):
    path = make_file(tmp_path / "LCA_Disclosure_Data_FY2026_Q3.xlsx")
    summaries, rows = lca.summarize(path)
    lca.save(conn, summaries, path, rows)
    note = lca.history_note(lca.lookup(conn, "testco"), path.name)
    assert "2 LCA filing(s)" in note and "1 for New York" in note and "FY2026 Q3" in note
    assert "no LCA filings found" in lca.history_note(None, path.name)
