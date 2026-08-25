"""Unit tests for the new-notice detection logic."""

import datetime
from pathlib import Path

import check_new_polls as cnp


ROOT = Path(__file__).resolve().parents[1]

CATALOG_SAMPLE = """filename,categorie,year,name,url,pdf creation-date
10239-pres-toluna.pdf,Pres,2026.0,10239 Pres TOLUNA HARRIS RTL,http://x/10239.pdf,2026-08-21 18:57:18+02:00
10238-pres-barometre-yougov.pdf,Pres,2026.0,10238 Pres Barometre politique YOUGOV,http://x/10238.pdf,2026-08-06 16:58:20+02:00
9001-leg-ifop.pdf,Leg,2026.0,9001 Leg IFOP,http://x/9001.pdf,2026-08-10 10:00:00+02:00
10100-pres-vieux.pdf,Pres,2025.0,10100 Pres Vieux sondage,http://x/10100.pdf,2025-01-05 10:00:00+01:00
"""


def notice(filename, name="", created="2026-08-21 10:00:00+02:00"):
    return {"filename": filename, "name": name, "pdf creation-date": created, "categorie": "Pres"}


class TestParseCatalog:
    def test_keeps_only_presidential_notices(self):
        entries = cnp.parse_catalog(CATALOG_SAMPLE)
        assert [e["filename"] for e in entries] == [
            "10239-pres-toluna.pdf",
            "10238-pres-barometre-yougov.pdf",
            "10100-pres-vieux.pdf",
        ]

    def test_reads_the_creation_date(self):
        entries = cnp.parse_catalog(CATALOG_SAMPLE)
        assert cnp.notice_date(entries[0]) == datetime.date(2026, 8, 21)

    def test_falls_back_when_no_date_is_readable(self):
        assert cnp.notice_date(notice("x.pdf", created="")) is None


class TestSelectNewNotices:
    def test_skips_notices_already_known(self):
        catalog = cnp.parse_catalog(CATALOG_SAMPLE)
        selected = cnp.select_new_notices(catalog, {"10239-pres-toluna.pdf"}, datetime.date(2026, 8, 1))
        assert [e["filename"] for e in selected] == ["10238-pres-barometre-yougov.pdf"]

    def test_skips_notices_older_than_the_floor(self):
        catalog = cnp.parse_catalog(CATALOG_SAMPLE)
        selected = cnp.select_new_notices(catalog, set(), datetime.date(2026, 8, 10))
        assert [e["filename"] for e in selected] == ["10239-pres-toluna.pdf"]

    def test_returns_oldest_first(self):
        catalog = cnp.parse_catalog(CATALOG_SAMPLE)
        selected = cnp.select_new_notices(catalog, set(), datetime.date(2020, 1, 1))
        assert [cnp.notice_date(e) for e in selected] == sorted(cnp.notice_date(e) for e in selected)

    def test_without_a_floor_everything_unknown_is_returned(self):
        catalog = cnp.parse_catalog(CATALOG_SAMPLE)
        assert len(cnp.select_new_notices(catalog, set(), None)) == 3


class TestKnownFilenames:
    def test_reads_filenames_from_polls_csv(self):
        tracked = cnp.tracked_filenames(ROOT / "polls.csv")
        assert tracked, "polls.csv should reference at least one source notice"
        assert all(name.endswith(".pdf") for name in tracked)

    def test_extracts_filenames_from_issue_bodies(self):
        issues = [
            {"body": "## Nouveau sondage\n\n**Fichier PDF à vérifier:** `10239-pres-toluna.pdf`\n"},
            {"body": "unrelated issue"},
            {"body": None},
        ]
        assert cnp.filenames_in_issues(issues) == {"10239-pres-toluna.pdf"}

    def test_a_closed_issue_still_counts_as_reported(self):
        """Closing an issue as out of scope is a decision; it must not be undone."""
        body = cnp.build_issue_body(notice("10238-pres-barometre-yougov.pdf"), "MieuxVoter/presidentielle2027")
        assert cnp.filenames_in_issues([{"body": body, "state": "closed"}]) == {"10238-pres-barometre-yougov.pdf"}


class TestScopeHeuristic:
    def test_flags_popularity_barometers(self):
        assert cnp.looks_out_of_scope(notice("x.pdf", name="10238 Pres Barometre politique YOUGOV"))
        assert cnp.looks_out_of_scope(notice("10232-pres-indices-popularite-ifop.pdf"))

    def test_leaves_voting_intention_notices_alone(self):
        assert not cnp.looks_out_of_scope(notice("10239-pres-toluna.pdf", name="10239 Pres TOLUNA HARRIS RTL"))

    def test_the_hint_reaches_the_issue_body(self):
        flagged = cnp.build_issue_body(notice("x.pdf", name="Barometre politique"), "owner/repo")
        plain = cnp.build_issue_body(notice("y.pdf", name="TOLUNA HARRIS RTL"), "owner/repo")
        assert "baromètre de popularité" in flagged
        assert "baromètre de popularité" not in plain


class TestIssueBody:
    def test_body_carries_the_filename_marker_the_parser_expects(self):
        body = cnp.build_issue_body(notice("10239-pres-toluna.pdf"), "owner/repo")
        assert cnp.FILENAME_IN_ISSUE.search(body).group(1) == "10239-pres-toluna.pdf"
