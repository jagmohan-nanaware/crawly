"""Offline contract tests for cold-start OA discovery and API diagnostics."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import requests

from crawly_core.discovery import (
    _add_openalex_locations, _clean, _discover_openalex,
    _discover_semantic_scholar, _get_json, _matching_paper, discover_oa_sources,
)
from crawly_core.downloader import download_paper_pdf
from crawly_core.models import AppConfig, Paper
from crawly_core.source_registry import CandidateSource, SourceRegistry


TITLE = "Mixed criticality on controller area network"
DOI = "10.1109/ECRTS.2013.23"


def response(code=200, payload=None):
    mock = MagicMock(status_code=code)
    mock.json.return_value = payload or {}
    return mock


def openalex_item(*, title=TITLE, doi="https://doi.org/" + DOI):
    return {
        "title": title, "publication_year": 2013, "doi": doi,
        "authorships": [{"author": {"display_name": "Alan Burns"}}],
        "best_oa_location": {
            "pdf_url": "https://york.ac.uk/first.pdf",
            "landing_page_url": "https://york.ac.uk/landing",
            "is_oa": True,
        },
        "primary_location": {
            "pdf_url": "https://journal.org/restricted.pdf",
            "is_oa": False,
        },
        "locations": [
            {"pdf_url": "https://york.ac.uk/first.pdf", "is_oa": True},
            {"pdf_url": "https://university.edu/accepted.pdf", "is_oa": True},
            {"landing_page_url": "https://repository.edu/item", "is_oa": True},
            {"pdf_url": "https://journal.org/restricted.pdf", "is_oa": False},
        ],
        "open_access": {"oa_url": "https://another.edu/fulltext"},
    }


def semantic_paper(*, title=TITLE, year=2013):
    return {
        "title": title, "year": year, "authors": [{"name": "Alan Burns"}],
        "openAccessPdf": {"url": "https://s2.org/public.pdf"},
        "externalIds": {"ArXiv": "2303.01234"},
    }


class TestColdstartDiscovery(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.registry = SourceRegistry(Path(self.tmp.name) / "sources.sqlite")
        self.cfg = AppConfig()
        self.paper = Paper(
            title=TITLE, authors="A. Burns and R.I. Davis", year=2013,
            extra_fields={"MCS Ref No": "158"},
        )

    def test_match_requires_title_year_and_author_when_available(self):
        item = openalex_item()
        self.assertTrue(_matching_paper(self.paper, item, "openalex"))
        self.assertFalse(_matching_paper(self.paper, {"title": ""}, "openalex"))
        self.assertFalse(_matching_paper(Paper("", authors="X"), item, "openalex"))
        self.assertFalse(_matching_paper(self.paper, openalex_item(title="Unrelated paper about cars"), "openalex"))
        self.assertFalse(_matching_paper(self.paper, {**item, "publication_year": 2017}, "openalex"))
        self.assertFalse(_matching_paper(self.paper, {**item, "publication_year": "bad"}, "openalex"))
        self.assertFalse(_matching_paper(
            self.paper, {**item, "authorships": [{"author": {"display_name": "Michael Smith"}}]}, "openalex"
        ))
        self.assertTrue(_matching_paper(self.paper, {**item, "authorships": []}, "openalex"))
        self.assertEqual(_clean("Mixed–Criticálity"), "mixed criticality")
        self.assertTrue(_matching_paper(self.paper, semantic_paper(), "semantic"))
        self.assertFalse(_matching_paper(self.paper, semantic_paper(year=2018), "semantic"))
        self.assertTrue(_matching_paper(Paper(TITLE), semantic_paper(year=None), "semantic"))

    def test_collect_every_oa_location_not_only_first(self):
        found = []
        _add_openalex_locations(openalex_item(), "158", found)
        by_url = {x.url: x for x in found}
        self.assertIn("https://york.ac.uk/first.pdf", by_url)
        self.assertIn("https://university.edu/accepted.pdf", by_url)
        self.assertIn("https://repository.edu/item", by_url)
        self.assertIn("https://another.edu/fulltext", by_url)
        self.assertNotIn("https://journal.org/restricted.pdf", by_url)
        self.assertEqual(len(found), len(by_url))
        self.assertEqual(by_url["https://york.ac.uk/first.pdf"].source_type, "discovered_oa")
        self.assertEqual(by_url["https://repository.edu/item"].source_type, "landing_page")

    def test_location_filters_duplicate_invalid_and_missing_links(self):
        loc = {"best_oa_location": {"pdf_url": "file:///tmp/bad.pdf", "landing_page_url": ""}, 
               "primary_location": {},
               "locations": [
                   {"is_oa": True, "pdf_url": "not-a-url"},
                   {"is_oa": False, "pdf_url": "https://private.org/private.pdf"},
                   {"is_oa": True, "pdf_url": "https://new.org/document.pdf"},
                   {"is_oa": True, "pdf_url": "https://new.org/document.pdf"},
                   {},
               ],
               "open_access": {"oa_url": "not-a-url"}}
        collected = []
        _add_openalex_locations(loc, "158", collected)
        self.assertEqual(len(collected), 1)
        self.assertEqual(collected[0].url, "https://new.org/document.pdf")
        item = {"open_access": {"oa_url": "https://oa.example.com/read"}}
        _add_openalex_locations(item, "158", collected)
        self.assertEqual(collected[-1].source_type, "landing_page")

    def test_provider_http_status_and_parse_error_preserved(self):
        codes = [(401, "authentication_required"), (403, "access_denied"),
                 (404, "not_found"), (429, "rate_limited"), (500, "http_error")]
        for code, expected in codes:
            with patch("crawly_core.discovery.requests.get", return_value=response(code)):
                self.assertIsNone(_get_json(
                    self.registry, "158", "OpenAlex", "https://api.example.org", None, self.cfg,
                ))
            self.assertEqual(self.registry.list_discovery_events("158")[-1]["outcome"], expected)
        with patch("crawly_core.discovery.requests.get", return_value=response(200, ["not-a-dict"])):
            self.assertIsNone(_get_json(self.registry, "158", "OpenAlex", "https://api.x", {}, self.cfg))
        self.assertEqual(self.registry.list_discovery_events("158")[-1]["outcome"], "invalid_response")
        with patch("crawly_core.discovery.requests.get", side_effect=requests.Timeout("slow")):
            self.assertIsNone(_get_json(self.registry, "158", "OpenAlex", "https://api.x", {}, self.cfg))
        self.assertEqual(self.registry.list_discovery_events("158")[-1]["outcome"], "request_error")
        mock = response(200)
        mock.json.side_effect = ValueError("bad json")
        with patch("crawly_core.discovery.requests.get", return_value=mock):
            self.assertIsNone(_get_json(self.registry, "158", "OpenAlex", "https://api.x", {}, self.cfg))
        self.assertEqual(self.registry.list_discovery_events("158")[-1]["detail"], "ValueError")
        with patch("crawly_core.discovery.requests.get", return_value=response(200, {"results": []})):
            self.assertEqual(_get_json(self.registry, "158", "OpenAlex", "https://api.x", {}, self.cfg),
                             {"results": []})

    def test_openalex_doi_lookups_and_title_fallback(self):
        self.paper.doi = DOI
        with patch("crawly_core.discovery.requests.get", return_value=response(200, openalex_item())) as req:
            found = _discover_openalex(self.paper, self.cfg, self.registry, "158")
        self.assertTrue(any(x.url.endswith("accepted.pdf") for x in found))
        self.assertIn(DOI, req.call_args.args[0])
        self.assertEqual(self.registry.list_discovery_events("158")[-1]["outcome"], "matched")

        # DOI is stale/unavailable, but title search finds an OA record.
        with patch("crawly_core.discovery.requests.get", side_effect=[
            response(404), response(200, {"results": [openalex_item()]})
        ]) as req:
            found = _discover_openalex(self.paper, self.cfg, self.registry, "158")
        self.assertTrue(found)
        self.assertEqual(req.call_count, 2)
        self.assertTrue(req.call_args.kwargs["params"]["search"])

    def test_openalex_title_query_populates_missing_doi_and_no_match(self):
        with patch("crawly_core.discovery.requests.get", return_value=response(200, {
            "results": [
                openalex_item(title="Entirely different paper on unrelated topic"),
                openalex_item(),
            ]
        })) as req:
            found = _discover_openalex(self.paper, self.cfg, self.registry, "158")
        self.assertEqual(self.paper.doi, DOI.lower())
        self.assertTrue(found)
        self.assertEqual(req.call_args.kwargs["params"]["per-page"], 5)
        blank = Paper("No such manuscript", authors="Nobody", year=2024)
        with patch("crawly_core.discovery.requests.get", return_value=response(200, {"results": [openalex_item()]})):
            self.assertEqual(_discover_openalex(blank, self.cfg, self.registry, "none"), [])
        self.assertEqual(self.registry.list_discovery_events("none")[-1]["outcome"], "no_match")

    def test_openalex_http_failure_no_candidates(self):
        with patch("crawly_core.discovery.requests.get", return_value=response(429)):
            self.assertEqual(_discover_openalex(self.paper, self.cfg, self.registry, "158"), [])
        self.assertEqual(self.registry.list_discovery_events("158")[-1]["outcome"], "rate_limited")

    def test_semantic_scholar_title_search_and_invalid_rows(self):
        data = {"data": [
            {"title": "Unrelated article about robotics", "year": 2013},
            semantic_paper(),
            {**semantic_paper(), "openAccessPdf": {"url": "invalid"}, "externalIds": {"ArXiv": "!invalid"}},
            {**semantic_paper(), "openAccessPdf": {"url": "https://s2.org/public.pdf"}, "externalIds": {}},
            {"title": "", "year": 2010},
            "bad row",
        ]}
        with patch("crawly_core.discovery.requests.get", return_value=response(200, data)) as req:
            found = _discover_semantic_scholar(self.paper, self.cfg, self.registry, "158")
        urls = [c.url for c in found]
        self.assertIn("https://s2.org/public.pdf", urls)
        self.assertIn("https://arxiv.org/pdf/2303.01234.pdf", urls)
        self.assertEqual(len(urls), len(set(urls)))
        self.assertEqual(req.call_args.kwargs["params"]["query"], TITLE)
        self.assertEqual(self.registry.list_discovery_events("158")[-1]["outcome"], "matched")

    def test_semantic_scholar_no_result_http_failure(self):
        with patch("crawly_core.discovery.requests.get", return_value=response(200, {"data": []})):
            self.assertEqual(_discover_semantic_scholar(self.paper, self.cfg, self.registry, "158"), [])
        self.assertEqual(self.registry.list_discovery_events("158")[-1]["outcome"], "no_match")
        with patch("crawly_core.discovery.requests.get", return_value=response(429)):
            self.assertEqual(_discover_semantic_scholar(self.paper, self.cfg, self.registry, "158"), [])
        self.assertEqual(self.registry.list_discovery_events("158")[-1]["outcome"], "rate_limited")

    def test_discovery_deduplicates_sources_and_saves_provenance(self):
        with patch("crawly_core.discovery.requests.get", side_effect=[
            response(200, {"results": [openalex_item(doi="")]}),
            response(200, {"data": [semantic_paper()]}),
            response(200, {"results": [openalex_item(doi="")]}),
            response(200, {"data": [semantic_paper()]}),
        ]):
            a = discover_oa_sources(self.paper, self.cfg, self.registry, "158")
            b = discover_oa_sources(self.paper, self.cfg, self.registry, "158")
        self.assertGreater(a, 0)
        self.assertEqual(b, 0)
        self.assertEqual(len(self.registry.list_discovery_events("158")), 4)
        self.assertTrue(all(c.reference_id == "158" for c in self.registry.list_sources("158")))

    def test_downloader_tries_discovered_oa_candidate_from_search(self):
        # Integration contract: a cold-start paper has no CSV PDF URL and yet
        # the registry gains a candidate before attempting fallback downloads.
        candidate = "https://university.edu/accepted.pdf"
        calls = []
        def fake_discover(paper, cfg, registry, ref):
            registry.upsert(CandidateSource(ref, candidate, source_type="discovered_oa",
                                            discovered_via="OpenAlex OA"))
            return 1
        def fake_fetch(url, path, **kwargs):
            calls.append(url)
            kwargs["status_callback"]("pdf_received")
            path.write_bytes(b"%PDF-1.4\n" + b"X" * 1500)
            return True
        with (
            patch("crawly_core.downloader.resolve_paper_metadata"),
            patch("crawly_core.downloader.discover_oa_sources", side_effect=fake_discover),
            patch("crawly_core.downloader._fetch_and_verify_pdf", side_effect=fake_fetch),
            patch("crawly_core.downloader._candidate_europe_pmc_urls", return_value=[]),
            patch("crawly_core.downloader._candidate_semantic_scholar_urls", return_value=[]),
        ):
            result = download_paper_pdf(self.paper, Path(self.tmp.name) / "pdfs",
                                        AppConfig(use_source_registry=True))
        self.assertTrue(result[0])
        self.assertEqual(calls, [candidate])
        self.assertIn("OpenAlex OA", result[1])


if __name__ == "__main__":
    unittest.main()
