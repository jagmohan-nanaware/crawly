"""Offline tests for the persistent multi-source confidence registry."""

import math
import tempfile
import unittest
from pathlib import Path

from crawly_core.models import Paper
from crawly_core.source_registry import (
    CandidateSource, SourceRegistry, candidates_from_paper,
)


class TestSourceRegistry(unittest.TestCase):
    def test_candidate_validation_and_ranking(self):
        normal = CandidateSource("753", "https://university.edu/Vestal.pdf")
        self.assertAlmostEqual(normal.priority_score, 0.25)
        self.assertEqual(normal.source_type, "unknown")
        for reference, url in (
            ("", "https://example.org/p.pdf"),
            ("753", "file:///tmp/p.pdf"),
            ("753", "javascript:alert(1)"),
            ("753", "http:///missing-host"),
        ):
            with self.subTest(reference=reference, url=url):
                with self.assertRaises(ValueError):
                    CandidateSource(reference, url)
        for field in ("access_confidence", "identity_confidence"):
            for value in (-0.1, 1.1, math.nan, math.inf):
                with self.subTest(field=field, value=value):
                    with self.assertRaises(ValueError):
                        CandidateSource("1", "https://example.org/doc.pdf", **{field: value})
        for value in (0, -1, math.nan, math.inf):
            with self.assertRaises(ValueError):
                CandidateSource("1", "https://example.org/doc.pdf", expected_cost=value)
        for attempts, responses, verified in ((0, 1, 0), (1, 0, 1), (1, 2, 0), (-1, 0, 0)):
            with self.assertRaises(ValueError):
                CandidateSource("1", "https://example.org/doc.pdf",
                                attempts=attempts, pdf_responses=responses,
                                verified_downloads=verified)

    def test_imports_multiple_csv_sources_without_discarding_metadata(self):
        paper = Paper(
            title="Preemptive scheduling",
            doi="10.1109/RTSS.2007.47",
            pdf_url="https://repository.edu/vestal.pdf",
            url="https://doi.org/10.1109/RTSS.2007.47",
            extra_fields={
                "MCS Ref No": "753",
                "MCS Sections": "1; 2; 4; 13",
                "Expected Filename": "753-1+2+4+13-Vestal-14.pdf",
                "Source URL 2": "https://author.edu/preprints/Vestal.pdf",
                "PDF URL 3": "https://repository.edu/vestal.pdf",
                "Source URL 4": "",
                "Access Confidence 1": "0.9",
                "Identity Confidence 1": "0.8",
                "Expected Cost 1": "2",
                "Access Confidence 2": "0.7",
                "Identity Confidence 2": "0.9",
                "Expected Cost 2": "1.5",
            },
        )
        sources = candidates_from_paper(paper)
        self.assertEqual(len(sources), 3)
        self.assertEqual([s.source_type for s in sources], [
            "primary_pdf", "alternate", "landing_page",
        ])
        self.assertEqual(sources[0].reference_id, "753")
        self.assertAlmostEqual(sources[0].priority_score, 0.36)
        self.assertEqual(sources[1].access_confidence, 0.7)
        self.assertEqual(sources[1].identity_confidence, 0.9)
        self.assertEqual(sources[2].expected_cost, 2.0)
        self.assertEqual(paper.extra_fields["Expected Filename"], "753-1+2+4+13-Vestal-14.pdf")

        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "nested" / "registry.sqlite"
            registry = SourceRegistry(db_path)
            self.assertEqual(registry.import_paper(paper), 3)
            listed = registry.list_sources("753")
            self.assertEqual(len(listed), 3)
            self.assertEqual(listed[0].url, "https://author.edu/preprints/Vestal.pdf")
            self.assertEqual(registry.list_sources("missing"), [])
            self.assertTrue(db_path.exists())
            self.assertEqual(registry.import_paper(paper), 3)
            self.assertEqual(len(registry.list_sources("753")), 3)

    def test_record_outcomes_and_preserve_evidence_on_upsert(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "registry.sqlite"
            registry = SourceRegistry(db_path)
            url = "https://author.edu/vestal.pdf"
            source = CandidateSource("753", url, access_confidence=0.8,
                                     identity_confidence=0.9, expected_cost=1.0)
            registry.upsert(source)
            for outcome in (
                "rate_limited", "not_found", "authentication_required", "access_denied",
                "download_error", "pdf_received", "identity_mismatch", "verified",
            ):
                registry.record_attempt("753", url, outcome)
            saved = registry.list_sources("753")[0]
            self.assertEqual(saved.attempts, 8)
            self.assertEqual(saved.pdf_responses, 3)
            self.assertEqual(saved.verified_downloads, 1)
            self.assertEqual(saved.last_outcome, "verified")
            self.assertLess(saved.priority_score, source.priority_score)
            registry.upsert(CandidateSource(
                "753", url, source_type="institutional", discovered_via="openalex",
                access_confidence=0.6, identity_confidence=0.7, expected_cost=2,
            ))
            updated = SourceRegistry(db_path).list_sources("753")[0]
            self.assertEqual(updated.attempts, 8)
            self.assertEqual(updated.verified_downloads, 1)
            self.assertEqual(updated.source_type, "institutional")
            self.assertEqual(updated.discovered_via, "openalex")
            self.assertEqual(updated.access_confidence, 0.6)

            with self.assertRaises(ValueError):
                registry.record_attempt("753", url, "unknown_status")
            with self.assertRaises(KeyError):
                registry.record_attempt("753", "https://missing.org/other.pdf", "not_found")

    def test_reference_fallback_and_empty_paper(self):
        by_doi = Paper(title="Demo", doi="10.1000/abc",
                       pdf_url="https://example.org/doc.pdf")
        self.assertEqual(candidates_from_paper(by_doi)[0].reference_id, "10.1000/abc")
        by_title = Paper(title="Title", pdf_url="https://example.org/title.pdf")
        self.assertEqual(candidates_from_paper(by_title)[0].reference_id, "Title")
        nothing = Paper(title="Title", url="NA")
        self.assertEqual(candidates_from_paper(nothing), [])
        duplicate = Paper(
            title="Title",
            pdf_url="https://example.org/title.pdf",
            url="https://example.org/title.pdf",
            extra_fields={"Source URL 2": "https://example.org/title.pdf"},
        )
        self.assertEqual(len(candidates_from_paper(duplicate)), 1)


if __name__ == "__main__":
    unittest.main()
