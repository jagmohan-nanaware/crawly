"""Offline regression tests for institutional and author-hosted discovery."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from crawly_core.discovery import _get_json
from crawly_core.downloader import download_paper_pdf
from crawly_core.models import AppConfig, Paper
from crawly_core.repository_discovery import (
    _discover_author_web, _discover_hal, _discover_openaire,
    _search_result_matches, _urls, discover_repository_sources,
)
from crawly_core.source_registry import CandidateSource, SourceRegistry


TITLE = "Mixed criticality on controller area network"
PDF = "https://www-users.york.ac.uk/~rd17/papers/MC_CAN.pdf"


def hal_record(**extras):
    return {
        "title_s": [TITLE], "authFullName_s": ["Alan Burns", "Robert Davis"],
        "producedDateY_i": 2013, "openAccess_bool": True,
        "fileMain_s": PDF,
        "uri_s": "https://hal.science/hal-12345",
        **extras,
    }


def openaire_record(**extras):
    return {
        "mainTitle": TITLE, "publicationDate": "2013-01-01",
        "authors": [{"fullName": "Alan Burns"}, {"fullName": "Robert Davis"}],
        "instances": [
            {
                "accessRight": {"label": "Open Access"},
                "urls": [
                    PDF,
                    "https://york.ac.uk/publications/accepted-paper",
                    "https://doi.org/10.1109/ECRTS.2013.23",
                ],
            },
            {"accessRight": {"label": "Closed Access"},
             "urls": ["https://publisher.example.org/restricted.pdf"]},
        ],
        **extras,
    }


class TestRepositoryDiscovery(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.registry = SourceRegistry(self.root / "registry.sqlite")
        self.cfg = AppConfig()
        self.paper = Paper(TITLE, authors="A. Burns and R.I. Davis", year=2013,
                           extra_fields={"MCS Ref No": "158"})

    def test_url_candidates_are_validated_deduplicated_and_classified(self):
        items = _urls("158", [
            PDF, PDF, "file:///etc/passwd", "", "mailto:user@york.ac.uk",
            "https://doi.org/10.1000/doc", 123, "https:///no-host",
            "http://127.0.0.1:8080/admin.pdf", "http://169.254.169.254/metadata",
            "https://10.0.0.1/copy.pdf", "http://localhost/file.pdf",
            "https://example.local/copy.pdf", "https://example.internal/private",
            "https://user:password@example.org/private.pdf",
            "https://public.example.org/another.pdf",
        ], "HAL repository", True)
        self.assertEqual(len(items), 3)
        self.assertEqual(items[0].source_type, "discovered_oa")
        self.assertEqual(items[1].source_type, "landing_page")
        self.assertEqual(items[0].discovered_via, "HAL repository")
        self.assertEqual(items[0].reference_id, "158")
        self.assertEqual(items[0].priority_score > items[1].priority_score, True)
        with patch("crawly_core.repository_discovery.CandidateSource", side_effect=ValueError("bad URL")):
            self.assertEqual(_urls("158", [PDF], "HAL", True), [])

    def test_hal_collects_author_pdf_and_landing_when_metadata_matches(self):
        with patch("crawly_core.repository_discovery._get_json", return_value={
            "response": {"docs": [
                {"title_s": ["Different manuscript"], "authFullName_s": ["Someone"]},
                hal_record(), hal_record(fileMain_s=""),
                "invalid",
            ]}
        }) as get:
            found = _discover_hal(self.paper, self.cfg, self.registry, "158")
        self.assertEqual(len(found), 3)
        self.assertEqual([c.source_type for c in found],
                         ["discovered_oa", "landing_page", "landing_page"])
        self.assertEqual(get.call_args.args[2], "HAL")
        self.assertIn('title_t:"Mixed criticality', get.call_args.args[4]["q"])
        self.assertEqual(self.registry.list_discovery_events("158")[-1]["outcome"], "matched")

    def test_hal_missing_fields_bad_title_and_api_error(self):
        with patch("crawly_core.repository_discovery._get_json", return_value={
            "response": {"docs": [
                hal_record(title_s=[], fileMain_s="", uri_s=""),
                hal_record(producedDateY_i=2019),
                hal_record(title_s=TITLE, authFullName_s=[], fileMain_s="", uri_s=""),
            ]}
        }):
            found = _discover_hal(self.paper, self.cfg, self.registry, "158")
        self.assertEqual(found, [])
        self.assertEqual(self.registry.list_discovery_events("158")[-1]["outcome"], "matched")
        with patch("crawly_core.repository_discovery._get_json", return_value=None):
            self.assertEqual(_discover_hal(self.paper, self.cfg, self.registry, "158"), [])
        with patch("crawly_core.repository_discovery._get_json", return_value={"response": {}}):
            self.assertEqual(_discover_hal(self.paper, self.cfg, self.registry, "158"), [])
        self.assertEqual(self.registry.list_discovery_events("158")[-1]["outcome"], "no_match")

    def test_openaire_oa_instances_and_exclusion_of_closed_copies(self):
        with patch("crawly_core.repository_discovery._get_json", return_value={
            "results": [
                openaire_record(),
                openaire_record(mainTitle="Unrelated document"),
                openaire_record(instances=[None, {"accessRight": {"label": "Restricted"},
                                                  "urls": [PDF]}]),
                "bad",
            ]
        }) as get:
            found = _discover_openaire(self.paper, self.cfg, self.registry, "158")
        self.assertEqual(get.call_args.args[2], "OpenAIRE")
        self.assertIn("mailto", get.call_args.args[4])
        self.assertEqual(len(found), 3)
        self.assertEqual(found[0].source_type, "discovered_oa")
        self.assertEqual(found[1].source_type, "landing_page")
        self.assertEqual(found[2].source_type, "landing_page")
        self.assertFalse(any("restricted" in c.url for c in found))
        self.assertEqual(self.registry.list_discovery_events("158")[-1]["outcome"], "matched")

    def test_openaire_handles_no_public_copy_or_bad_date_and_http_failure(self):
        with patch("crawly_core.repository_discovery._get_json", return_value={
            "results": [
                openaire_record(publicationDate="invalid",
                                authors=[{"fullName": "Alan Burns"}, "bad"],
                                instances=[{"accessRight": {"label": "Closed Access"}, "urls": [PDF]}]),
                openaire_record(publicationDate="2019",
                                instances=[{"accessRight": {"label": "OPEN"}, "urls": [PDF]}]),
                openaire_record(publicationDate="2013", instances=[
                    {"accessRight": {"label": "OPEN"}, "urls": [None, 42]},
                    {"accessRight": {}, "urls": [PDF]},
                ]),
            ]
        }):
            result = _discover_openaire(self.paper, self.cfg, self.registry, "158")
        self.assertEqual(result, [])
        with patch("crawly_core.repository_discovery._get_json", return_value=None):
            self.assertEqual(_discover_openaire(self.paper, self.cfg, self.registry, "158"), [])
        with patch("crawly_core.repository_discovery._get_json", return_value={"results": []}):
            self.assertEqual(_discover_openaire(self.paper, self.cfg, self.registry, "158"), [])
        self.assertEqual(self.registry.list_discovery_events("158")[-1]["outcome"], "no_match")

    def test_search_snippet_requires_distinctive_title_tokens(self):
        self.assertTrue(_search_result_matches(self.paper, {
            "title": "[PDF] Mixed Criticality on Controller Area Network | University of York"
        }))
        self.assertTrue(_search_result_matches(self.paper, {
            "title": "Mixed Criticality on Controller",
            "description": "Area Network published by York",
        }))
        self.assertFalse(_search_result_matches(self.paper, {"title": "A guide to automotive CAN"}))
        self.assertFalse(_search_result_matches(Paper(""), {"title": TITLE}))
        self.assertFalse(_search_result_matches(Paper("Tiny title"), {"title": "Tiny"}))
        self.assertFalse(_search_result_matches(self.paper, {"title": "", "description": ""}))

    def test_optional_web_pdf_search_matches_results_and_obeys_key(self):
        with patch.dict("os.environ", {"BRAVE_SEARCH_API_KEY": ""}):
            with patch("crawly_core.repository_discovery._get_json") as get:
                self.assertEqual(_discover_author_web(
                    self.paper, self.cfg, self.registry, "158"), [])
                get.assert_not_called()
        with patch.dict("os.environ", {"BRAVE_SEARCH_API_KEY": "demo-secret"}):
            with patch("crawly_core.repository_discovery._get_json", return_value={
                "web": {"results": [
                    {"title": "[PDF] " + TITLE, "url": PDF},
                    {"title": TITLE, "url": "https://author.edu/papers/accepted"},
                    {"title": "Computer vision unrelated paper", "url": "https://x.org/wrong.pdf"},
                    {"title": TITLE, "url": "not a URL"},
                    {"title": TITLE, "url": ""},
                    {"title": TITLE, "url": 42},
                    "bad",
                ]}
            }) as get:
                found = _discover_author_web(self.paper, self.cfg, self.registry, "158")
        self.assertEqual(len(found), 2)
        self.assertEqual(found[0].source_type, "discovered_oa")
        self.assertEqual(found[1].source_type, "landing_page")
        self.assertIn("filetype:pdf", get.call_args.args[4]["q"])
        self.assertNotIn("demo-secret", str(found))
        self.assertEqual(self.registry.list_discovery_events("158")[-1]["outcome"], "matched")

    def test_web_search_no_title_no_results_no_json(self):
        with patch.dict("os.environ", {"BRAVE_SEARCH_API_KEY": "demo-secret"}):
            with patch("crawly_core.repository_discovery._get_json") as get:
                self.assertEqual(_discover_author_web(Paper(""), self.cfg, self.registry, "158"), [])
                get.assert_not_called()
            with patch("crawly_core.repository_discovery._get_json", return_value=None):
                self.assertEqual(_discover_author_web(self.paper, self.cfg, self.registry, "158"), [])
            with patch("crawly_core.repository_discovery._get_json", return_value={"web": {}}):
                self.assertEqual(_discover_author_web(self.paper, self.cfg, self.registry, "158"), [])
        self.assertEqual(self.registry.list_discovery_events("158")[-1]["outcome"], "no_match")

    def test_brave_token_is_sent_only_in_header_and_not_persisted(self):
        cfg = AppConfig()
        response = MagicMock(status_code=200)
        response.json.return_value = {"web": {"results": []}}
        with (
            patch.dict("os.environ", {"BRAVE_SEARCH_API_KEY": "private-brave-key"}),
            patch("crawly_core.discovery.requests.get", return_value=response) as req,
        ):
            value = _get_json(self.registry, "158", "Brave",
                              "https://api.search.brave.com/res/v1/web/search",
                              {"q": '"test" filetype:pdf'}, cfg)
        self.assertIn("web", value)
        self.assertEqual(req.call_args.kwargs["headers"]["X-Subscription-Token"],
                         "private-brave-key")
        self.assertNotIn("private-brave-key", str(self.registry.list_discovery_events("158")))

    def test_repo_discovery_deduplicates_independent_sources(self):
        with (
            patch("crawly_core.repository_discovery._discover_hal", return_value=[
                CandidateSource("158", PDF, source_type="discovered_oa"),
            ]),
            patch("crawly_core.repository_discovery._discover_openaire", return_value=[
                CandidateSource("158", PDF),
                CandidateSource("158", "https://other.edu/paper.pdf"),
            ]),
            patch("crawly_core.repository_discovery._discover_author_web", return_value=[]),
        ):
            self.assertEqual(discover_repository_sources(
                self.paper, self.cfg, self.registry, "158"), 2)
            self.assertEqual(discover_repository_sources(
                self.paper, self.cfg, self.registry, "158"), 0)
        self.assertEqual(len(self.registry.list_sources("158")), 2)

    def test_repository_fallback_is_attempted_after_conventional_oa_fails(self):
        attempts = []
        def fake_discover(paper, cfg, registry, ref, blocked=None):
            registry.upsert(CandidateSource(
                ref, PDF, source_type="discovered_oa",
                discovered_via="HAL repository",
            ))
            return 1
        def fetch(url, path, **kwargs):
            attempts.append(url)
            kwargs["status_callback"]("pdf_received")
            path.write_bytes(b"%PDF-1.5\n" + b"x" * 2000)
            return True

        with (
            patch("crawly_core.downloader.resolve_paper_metadata"),
            patch("crawly_core.downloader.discover_oa_sources", return_value=0),
            patch("crawly_core.downloader.discover_repository_sources", side_effect=fake_discover),
            patch("crawly_core.downloader._candidate_europe_pmc_urls", return_value=[]),
            patch("crawly_core.downloader._fetch_and_verify_pdf", side_effect=fetch),
        ):
            ok, source, path = download_paper_pdf(
                self.paper, self.root / "pdfs", AppConfig(use_source_registry=True),
            )
        self.assertTrue(ok)
        self.assertEqual(source, "Registry: HAL repository")
        self.assertEqual(attempts, [PDF])
        self.assertTrue(path.exists())


if __name__ == "__main__":
    unittest.main()
