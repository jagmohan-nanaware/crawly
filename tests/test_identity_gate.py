"""Deterministic PDF fixtures and opt-in bibliographic identity gate tests."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from crawly_core.identity import _canonical_doi, _first_author, _normalize, assess_pdf_identity
from crawly_core.downloader import (
    _identity_check_and_archive, download_batch_pdfs, download_paper_pdf,
)
from crawly_core.models import AppConfig, Paper, load_papers_from_csv
from crawly_core.source_registry import CandidateSource, SourceRegistry


def make_pdf(path: Path, text: str, title: str = "", author: str = "") -> None:
    """Make a real, extractable PDF with only the project's pypdf dependency."""
    writer = PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    font = DictionaryObject({
        NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type1"),
        NameObject("/BaseFont"): NameObject("/Helvetica"),
    })
    fonts = DictionaryObject({NameObject("/F1"): writer._add_object(font)})
    page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): fonts})
    stream = DecodedStreamObject()
    safe_text = text.replace("\\", "").replace("(", "").replace(")", "")
    stream.set_data(
        ("BT /F1 12 Tf 72 720 Td (" + safe_text + ") Tj ET").encode("ascii")
    )
    page[NameObject("/Contents")] = writer._add_object(stream)
    writer.add_metadata({
        "/Title": title, "/Author": author,
        "/Subject": "A" * 1200,
    })
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as handle:
        writer.write(handle)


class TestBibliographicIdentity(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.title = "Preemptive scheduling of multi criticality systems"
        self.doi = "10.1109/RTSS.2007.47"
        self.paper = Paper(
            title=self.title, authors="Vestal, Steve",
            year=2007, doi=self.doi,
            pdf_url="https://author.example.org/Vestal.pdf",
            extra_fields={"MCS Ref No": "753"},
        )
        self.header = self.title + " Steve Vestal 2007 DOI: " + self.doi

    def test_helpers_and_verified_title_author_year(self):
        self.assertEqual(_normalize("  Étude / Mixed-Criticality! "), "etude mixed criticality")
        self.assertEqual(_first_author(self.paper), "vestal")
        self.assertEqual(_first_author(Paper("P", authors="Ada Lovelace and Bob Smith")), "lovelace")
        self.assertEqual(_first_author(Paper("P", authors="Unknown")), "")
        self.assertEqual(_canonical_doi("https://doi.org/10.1000/ABC.)"), "10.1000/abc")
        path = self.root / "right.pdf"
        make_pdf(path, self.header, self.title, "Steve Vestal")
        result = assess_pdf_identity(self.paper, path)
        self.assertEqual(result.status, "verified")
        self.assertTrue(result.evidence["title_found"])
        self.assertTrue(result.evidence["author_found"])
        self.assertTrue(result.evidence["year_found"])
        self.assertTrue(result.evidence["doi_found"])
        self.assertFalse(result.evidence["doi_conflict"])

    def test_verified_without_year_when_bibliography_year_unknown(self):
        path = self.root / "right.pdf"
        make_pdf(path, self.title + " Steve Vestal", self.title, "Steve Vestal")
        p = Paper(self.title, authors="Steve Vestal")
        self.assertEqual(assess_pdf_identity(p, path).status, "verified")

    def test_needs_review_when_title_or_author_or_year_insufficient(self):
        path = self.root / "missing_author.pdf"
        make_pdf(path, self.title + " 2007", self.title)
        a = assess_pdf_identity(self.paper, path)
        self.assertEqual(a.status, "needs_review")
        self.assertFalse(a.evidence["author_found"])
        make_pdf(path, "Completely unreadable article introduction", "Generic PDF")
        self.assertEqual(assess_pdf_identity(self.paper, path).status, "needs_review")
        make_pdf(path, self.title + " Steve Vestal", self.title, "Steve Vestal")
        self.assertEqual(assess_pdf_identity(self.paper, path).status, "needs_review")

    def test_mismatch_on_authoritative_title_or_header_doi(self):
        path = self.root / "wrong.pdf"
        wrong = "A different scientific paper about automotive testing"
        make_pdf(path, wrong + " Other Author 2018", wrong, "Other Author")
        a = assess_pdf_identity(self.paper, path)
        self.assertEqual(a.status, "mismatch")
        self.assertTrue(a.evidence["metadata_title_conflict"])
        make_pdf(
            path, self.title + " Steve Vestal 2007 DOI: 10.1000/different",
            self.title, "Steve Vestal",
        )
        b = assess_pdf_identity(self.paper, path)
        self.assertEqual(b.status, "mismatch")
        self.assertTrue(b.evidence["doi_conflict"])
        self.assertEqual(b.evidence["header_doi"], "10.1000/different")

    def test_broken_and_empty_pdf_require_review(self):
        bad_path = self.root / "broken.pdf"
        bad_path.write_bytes(b"invalid")
        self.assertEqual(
            assess_pdf_identity(self.paper, bad_path).evidence["reason"], "pdf_text_unavailable"
        )
        blank_path = self.root / "blank.pdf"
        writer = PdfWriter()
        with blank_path.open("wb") as handle:
            writer.write(handle)
        self.assertEqual(assess_pdf_identity(self.paper, blank_path).status, "needs_review")
        self.assertEqual(assess_pdf_identity(self.paper, self.root / "absent.pdf").status, "needs_review")

    def test_registered_sources_retry_after_mismatch_then_verify(self):
        cfg = AppConfig(use_source_registry=True, verify_pdf_identity=True)
        bad_url = "https://bad.example.org/article.pdf"
        good_url = "https://good.example.edu/article.pdf"
        self.paper.pdf_url = bad_url
        self.paper.extra_fields.update({
            "Source URL 2": good_url,
            "Access Confidence 1": "0.95",
            "Identity Confidence 1": "0.95",
            "Access Confidence 2": "0.55",
            "Identity Confidence 2": "0.55",
        })
        calls = []
        def fake_fetch(url, dest, **kwargs):
            calls.append(url)
            if url == bad_url:
                wrong = "Different academic paper about robotic manipulation"
                make_pdf(dest, wrong + " A Person 2019", wrong)
            else:
                make_pdf(dest, self.header, self.title, "Steve Vestal")
            kwargs["status_callback"]("pdf_received")
            return True

        output = self.root / "pdfs"
        csv_path = self.root / "res.csv"
        with (
            patch("crawly_core.downloader._fetch_and_verify_pdf", side_effect=fake_fetch),
            patch("crawly_core.downloader.resolve_paper_metadata") as resolve,
        ):
            stats = download_batch_pdfs([self.paper], output, csv_path=csv_path, config=cfg)
        resolve.assert_not_called()
        self.assertEqual(stats["downloaded"], 1)
        self.assertEqual(calls, [bad_url, good_url])
        self.assertEqual(self.paper.extra_fields["PDF Identity Status"], "verified")
        self.assertEqual(json.loads(self.paper.extra_fields["PDF Identity Evidence"])["reason"],
                         "title_author_and_year_or_doi")
        registry = SourceRegistry(output / "sources.sqlite")
        checks = {c["url"]: c for c in registry.list_identity_checks("753")}
        self.assertEqual(checks[bad_url]["status"], "mismatch")
        self.assertEqual(checks[good_url]["status"], "verified")
        sources = {c.url: c for c in registry.list_sources("753")}
        self.assertEqual(sources[bad_url].last_outcome, "identity_mismatch")
        self.assertEqual(sources[good_url].verified_downloads, 1)
        self.assertEqual(sources[good_url].pdf_responses, 1)
        self.assertTrue(list((output / "identity_review" / "mismatch").glob("*.pdf")))
        self.assertTrue((output / self.paper.safe_filename()).exists())
        self.assertEqual(load_papers_from_csv(csv_path)[0].extra_fields["PDF Identity Status"], "verified")

    def test_needs_review_quarantines_and_is_not_counted_as_success(self):
        cfg = AppConfig(verify_pdf_identity=True)
        def fake_fetch(url, target, **kwargs):
            make_pdf(target, self.title + " 2007", self.title)
            kwargs["status_callback"]("pdf_received")
            return True
        with (
            patch("crawly_core.downloader._fetch_and_verify_pdf", side_effect=fake_fetch),
            patch("crawly_core.downloader.resolve_paper_metadata"),
            patch("crawly_core.downloader._candidate_unpaywall_urls", return_value=[]),
            patch("crawly_core.downloader._candidate_semantic_scholar_urls", return_value=[]),
            patch("crawly_core.downloader._candidate_europe_pmc_urls", return_value=[]),
        ):
            stats = download_batch_pdfs([self.paper], self.root / "pdfs", config=cfg)
        self.assertEqual(stats["downloaded"], 0)
        self.assertEqual(stats["missing"], 1)
        self.assertEqual(self.paper.extra_fields["PDF Identity Status"], "needs_review")
        self.assertTrue(list((self.root / "pdfs" / "identity_review" / "needs_review").glob("*.pdf")))
        registry = SourceRegistry(self.root / "pdfs" / "sources.sqlite")
        self.assertEqual(registry.list_sources("753")[0].last_outcome, "pdf_received")
        self.assertEqual(registry.list_identity_checks("753")[0]["status"], "needs_review")

    def test_cached_file_must_be_verified_in_strict_mode(self):
        target_dir = self.root / "pdfs"
        target_dir.mkdir()
        target = target_dir / self.paper.safe_filename()
        make_pdf(target, self.header, self.title, "Steve Vestal")
        with patch("crawly_core.downloader._fetch_and_verify_pdf") as fetch:
            ok = download_paper_pdf(
                self.paper, target_dir, AppConfig(verify_pdf_identity=True),
            )
        self.assertTrue(ok[0])
        fetch.assert_not_called()
        self.assertEqual(self.paper.extra_fields["PDF Identity Status"], "verified")

        # A wrong cached PDF must not be accepted. Move it aside and attempt sources.
        wrong = "Different paper about industrial networking"
        make_pdf(target, wrong + " Someone 2019", wrong)
        with (
            patch("crawly_core.downloader._fetch_and_verify_pdf", return_value=False),
            patch("crawly_core.downloader.resolve_paper_metadata"),
            patch("crawly_core.downloader._candidate_unpaywall_urls", return_value=[]),
            patch("crawly_core.downloader._candidate_semantic_scholar_urls", return_value=[]),
            patch("crawly_core.downloader._candidate_europe_pmc_urls", return_value=[]),
        ):
            missing = download_paper_pdf(
                self.paper, target_dir, AppConfig(verify_pdf_identity=True),
            )
        self.assertFalse(missing[0])
        self.assertEqual(self.paper.local_pdf, "")
        self.assertTrue(list((target_dir / "identity_review" / "mismatch").glob("*.pdf")))

    def test_quarantined_files_do_not_overwrite_previous_evidence(self):
        destination = self.root / "paper.pdf"
        make_pdf(destination, "Totally different scientific article 2018",
                 "Totally different scientific article")
        a = _identity_check_and_archive(self.paper, destination, "https://x.org/a", None, "753")
        self.assertEqual(a, "mismatch")
        first = json.loads(self.paper.extra_fields["PDF Identity Evidence"])["review_pdf_path"]
        make_pdf(destination, "Totally different scientific article 2018",
                 "Totally different scientific article")
        _identity_check_and_archive(self.paper, destination, "https://x.org/a", None, "753")
        second = json.loads(self.paper.extra_fields["PDF Identity Evidence"])["review_pdf_path"]
        self.assertNotEqual(first, second)
        self.assertTrue(Path(first).exists())
        self.assertTrue(Path(second).exists())

    def test_registry_evidence_updates_and_validates_status(self):
        registry = SourceRegistry(self.root / "r.sqlite")
        url = "https://university.edu/paper.pdf"
        registry.upsert(CandidateSource("753", url))
        self.assertEqual(registry.list_identity_checks("753"), [])
        with self.assertRaises(ValueError):
            registry.record_identity_check("753", url, "unrecognized", {})
        registry.record_identity_check("753", url, "needs_review", {"reason": "no_text"})
        registry.record_identity_check("753", url, "verified", {"reason": "title_and_author"})
        checks = registry.list_identity_checks("753")
        self.assertEqual(len(checks), 1)
        self.assertEqual(checks[0]["status"], "verified")
        self.assertEqual(checks[0]["evidence"]["reason"], "title_and_author")
        self.assertTrue(checks[0]["checked_utc"].endswith("+00:00"))

    def test_cli_verify_identity_implies_registry(self):
        from crawly_core.cli import main
        from crawly_core.models import save_papers_to_csv

        csv_path = self.root / "sample.csv"
        save_papers_to_csv([self.paper], csv_path)
        with (
            patch("crawly_core.cli.AppConfig.load", return_value=AppConfig(ai_provider="extractive")),
            patch("crawly_core.cli.render_header"),
            patch("crawly_core.cli.download_batch_pdfs",
                  return_value={"total": 1, "downloaded": 0, "missing": 1}) as batch,
        ):
            self.assertEqual(main([
                "download", str(csv_path), "--verify-identity",
                "-o", str(self.root / "out"),
            ]), 0)
        cfg = batch.call_args.kwargs["config"]
        self.assertTrue(cfg.use_source_registry)
        self.assertTrue(cfg.verify_pdf_identity)

    def test_strict_scihub_fallback_is_also_identity_checked(self):
        cfg = AppConfig(verify_pdf_identity=True, use_scihub_fallback=True)
        with (
            patch("crawly_core.downloader.resolve_paper_metadata"),
            patch("crawly_core.downloader._candidate_unpaywall_urls", return_value=[]),
            patch("crawly_core.downloader._candidate_semantic_scholar_urls", return_value=[]),
            patch("crawly_core.downloader._candidate_europe_pmc_urls", return_value=[]),
            patch("crawly_core.downloader._fetch_and_verify_pdf", return_value=False),
            patch("crawly_core.downloader._try_scihub_download") as sh,
        ):
            wrong = "An entirely different paper published in 2015"
            def fake_mirror(paper, path, **kwargs):
                make_pdf(path, wrong + " Different Person 2015", wrong)
                return "Approved mirror"
            sh.side_effect = fake_mirror
            missing = download_paper_pdf(self.paper, self.root / "pdfs", cfg)
            self.assertFalse(missing[0])
            self.assertEqual(missing[1], "Identity Mismatch")
            sh.side_effect = lambda paper, path, **kw: (
                make_pdf(path, self.header, self.title, "Steve Vestal"), "Approved mirror"
            )[1]
            good = download_paper_pdf(self.paper, self.root / "pdfs", cfg)
            self.assertTrue(good[0])
            self.assertEqual(good[1], "Approved mirror")


if __name__ == "__main__":
    unittest.main()
