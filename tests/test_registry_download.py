"""Offline end-to-end checks for optional source-registry downloader integration."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from crawly_core.downloader import _fetch_and_verify_pdf, download_batch_pdfs, download_paper_pdf
from crawly_core.models import AppConfig, Paper, load_papers_from_csv, save_papers_to_csv
from crawly_core.source_registry import SourceRegistry


class TestRegisteredAcquisition(unittest.TestCase):
    def setUp(self):
        discovery = patch("crawly_core.downloader.discover_oa_sources", return_value=0)
        discovery.start()
        self.addCleanup(discovery.stop)

    def test_fetch_status_observer(self):
        cfg = AppConfig()
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "p.pdf"
            outcomes = []
            for code, expected in (
                (401, "authentication_required"),
                (403, "access_denied"),
                (404, "not_found"),
                (429, "rate_limited"),
                (503, "download_error"),
            ):
                with patch("crawly_core.downloader.requests.get", return_value=MagicMock(status_code=code)):
                    self.assertFalse(_fetch_and_verify_pdf(
                        "https://example.org/doc", target, config=cfg, status_callback=outcomes.append
                    ))
                self.assertEqual(outcomes.pop(), expected)

            pdf_bytes = b"%PDF-1.4\n" + b"0" * 2048
            resp = MagicMock(status_code=200, headers={"Content-Type": "application/pdf"})
            resp.iter_content.return_value = [pdf_bytes]
            with patch("crawly_core.downloader.requests.get", return_value=resp):
                self.assertTrue(_fetch_and_verify_pdf(
                    "https://example.org/doc", target, config=cfg, status_callback=outcomes.append
                ))
            self.assertEqual(outcomes.pop(), "pdf_received")

            resp = MagicMock(status_code=200, headers={"Content-Type": "text/plain"})
            resp.iter_content.return_value = [b"plain"]
            with patch("crawly_core.downloader.requests.get", return_value=resp):
                self.assertFalse(_fetch_and_verify_pdf(
                    "https://example.org/doc", target, config=cfg, status_callback=outcomes.append
                ))
            self.assertEqual(outcomes.pop(), "download_error")
            for invalid in ("", "file:///tmp/secret"):
                self.assertFalse(_fetch_and_verify_pdf(invalid, target, status_callback=outcomes.append))
                self.assertEqual(outcomes.pop(), "download_error")
            with patch("crawly_core.downloader.requests.get", side_effect=TimeoutError()):
                self.assertFalse(_fetch_and_verify_pdf(
                    "https://example.org/doc", target, config=cfg, status_callback=outcomes.append
                ))
            self.assertEqual(outcomes.pop(), "download_error")

            html = b'<html><embed id="pdf" src="/nested.pdf"></html>'
            outer = MagicMock(status_code=200, url="https://example.org/landing",
                              headers={"Content-Type": "text/html"})
            outer.iter_content.return_value = [html]
            for code, expected in ((403, "access_denied"), (429, "rate_limited")):
                nested = MagicMock(status_code=code)
                with patch("crawly_core.downloader.requests.get", side_effect=[outer, nested]):
                    self.assertFalse(_fetch_and_verify_pdf(
                        "https://example.org/landing", target, config=cfg,
                        status_callback=outcomes.append,
                    ))
                self.assertEqual(outcomes.pop(), expected)
            nested = MagicMock(status_code=200, content=pdf_bytes)
            with patch("crawly_core.downloader.requests.get", side_effect=[outer, nested]):
                self.assertTrue(_fetch_and_verify_pdf(
                    "https://example.org/landing", target, config=cfg,
                    status_callback=outcomes.append,
                ))
            self.assertEqual(outcomes.pop(), "pdf_received")
            self.assertEqual(len(outcomes), 0)

    def test_bad_csv_candidate_does_not_abort_batch(self):
        from crawly_core.source_registry import candidates_from_paper

        paper = Paper(
            title="A study", pdf_url="NA",
            extra_fields={
                "MCS Ref No": "40",
                "Source URL 2": "https://author.edu/valid.pdf",
                "Source URL 3": "file:///etc/passwd",
                "Source URL 4": "https://author.edu/invalid-score.pdf",
                "Access Confidence 4": "not-a-number",
                "Source URL 5": "https://author.edu/bad-range.pdf",
                "Identity Confidence 5": "1.5",
            },
        )
        candidates = candidates_from_paper(paper)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].url, "https://author.edu/valid.pdf")

    def test_registry_tried_before_metadata_api_and_persisted(self):
        with tempfile.TemporaryDirectory() as directory:
            cfg = AppConfig(use_source_registry=True, source_registry_path=str(Path(directory) / "sources.db"))
            output = Path(directory) / "pdfs"
            preferred = "https://author.example.org/article.pdf"
            p = Paper(
                title="Research paper", abstract="Original abstract", summary="Prior notes",
                pdf_url="https://publisher.example.org/article.pdf",
                extra_fields={
                    "MCS Ref No": "753",
                    "MCS Sections": "1; 2",
                    "Expected Filename": "753-Vestal-14.pdf",
                    "Source URL 2": preferred,
                    "Access Confidence 2": "0.99",
                    "Identity Confidence 2": "0.99",
                },
            )
            def fake_fetch(url, target_path, **kwargs):
                self.assertEqual(url, preferred)
                kwargs["status_callback"]("pdf_received")
                target_path.write_bytes(b"%PDF-1.4\n" + b"x" * 2048)
                return True
            with (
                patch("crawly_core.downloader._fetch_and_verify_pdf", side_effect=fake_fetch) as mocked,
                patch("crawly_core.downloader.resolve_paper_metadata") as resolve,
            ):
                stat = download_batch_pdfs([p], output, csv_path=Path(directory) / "records.csv", config=cfg)
            self.assertEqual(stat["downloaded"], 1)
            self.assertEqual(mocked.call_count, 1)
            resolve.assert_not_called()
            self.assertTrue((output / "753-Vestal-14.pdf").exists())
            self.assertEqual(p.download_source, "Registry: alternate")
            self.assertEqual(p.abstract, "Original abstract")
            self.assertEqual(p.summary, "Prior notes")
            registry = SourceRegistry(cfg.source_registry_path)
            stored = {c.url: c for c in registry.list_sources("753")}
            self.assertEqual(stored[preferred].pdf_responses, 1)
            self.assertEqual(stored[preferred].verified_downloads, 0)
            self.assertEqual(stored[preferred].last_outcome, "pdf_received")
            reread = load_papers_from_csv(Path(directory) / "records.csv")[0]
            self.assertEqual(reread.extra_fields["MCS Sections"], "1; 2")
            self.assertEqual(reread.abstract, "Original abstract")
            self.assertEqual(reread.summary, "Prior notes")

            with patch("crawly_core.downloader._fetch_and_verify_pdf") as second_fetch:
                cached = download_paper_pdf(p, output, config=cfg, registry=registry)
            self.assertTrue(cached[0])
            second_fetch.assert_not_called()

    def test_publisher_landing_is_deferred_until_after_oa_lookup(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = SourceRegistry(Path(directory) / "registry.sqlite")
            paper = Paper(
                title="Article with publisher landing page",
                doi="10.1000/defer",
                url="https://publisher.example.org/landing",
                extra_fields={"MCS Ref No": "100"},
            )
            attempted = []
            def fake_fetch(url, path, **kwargs):
                attempted.append(url)
                success = url == "https://repository.example.edu/open.pdf"
                kwargs["status_callback"]("pdf_received" if success else "access_denied")
                return success
            with (
                patch("crawly_core.downloader._fetch_and_verify_pdf", side_effect=fake_fetch),
                patch("crawly_core.downloader.resolve_paper_metadata") as resolve,
                patch("crawly_core.downloader._candidate_unpaywall_urls",
                      return_value=[("Unpaywall OA", "https://repository.example.edu/open.pdf")]),
                patch("crawly_core.downloader._candidate_semantic_scholar_urls", return_value=[]),
                patch("crawly_core.downloader._candidate_europe_pmc_urls", return_value=[]),
            ):
                result = download_paper_pdf(paper, Path(directory), AppConfig(), registry=registry)
            self.assertTrue(result[0])
            self.assertEqual(attempted, ["https://repository.example.edu/open.pdf"])
            resolve.assert_not_called()

    def test_landing_page_is_eventually_tried_when_no_other_source(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = SourceRegistry(Path(directory) / "registry.sqlite")
            paper = Paper(
                title="Article", url="https://repository.example.edu/landing",
                extra_fields={"MCS Ref No": "300"},
            )
            with (
                patch("crawly_core.downloader.resolve_paper_metadata") as resolve,
                patch("crawly_core.downloader._candidate_europe_pmc_urls", return_value=[]),
                patch("crawly_core.downloader._fetch_and_verify_pdf",
                      side_effect=lambda url, target, **kw: (kw["status_callback"]("pdf_received"), True)[1]) as fetch,
            ):
                result = download_paper_pdf(paper, Path(directory), AppConfig(), registry=registry)
            self.assertTrue(result[0])
            resolve.assert_called_once()
            self.assertEqual(fetch.call_count, 1)
            self.assertEqual(registry.list_sources("300")[0].last_outcome, "pdf_received")

    def test_missing_primary_uses_discovered_open_access(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = SourceRegistry(Path(directory) / "sources.sqlite")
            cfg = AppConfig(use_source_registry=True)
            p = Paper(title="Fallback", pdf_url="https://publisher.example.com/blocked",
                      extra_fields={"MCS Ref No": "607"})
            seen = []
            def fake_fetch(url, path, **kwargs):
                seen.append(url)
                ok = "oa.example.edu" in url
                kwargs["status_callback"]("pdf_received" if ok else "not_found")
                return ok
            with (
                patch("crawly_core.downloader._fetch_and_verify_pdf", side_effect=fake_fetch),
                patch("crawly_core.downloader.resolve_paper_metadata") as resolver,
                patch("crawly_core.downloader._candidate_unpaywall_urls", return_value=[
                    ("Unpaywall OA", "https://oa.example.edu/paper.pdf"),
                    ("Duplicate", "https://oa.example.edu/paper.pdf"),
                ]),
                patch("crawly_core.downloader._candidate_semantic_scholar_urls", return_value=[]),
                patch("crawly_core.downloader._candidate_europe_pmc_urls", return_value=[]),
            ):
                p.doi = "10.1007/example"
                result = download_paper_pdf(p, Path(directory), cfg, registry=registry)
            self.assertTrue(result[0])
            self.assertEqual(result[1], "Registry: Unpaywall OA")
            self.assertEqual(len(seen), 2)
            resolver.assert_not_called()
            self.assertEqual(registry.list_sources("607")[0].last_outcome, "pdf_received")
            self.assertEqual(len(registry.list_sources("607")), 2)

    def test_rate_limit_prevents_same_host_retry_across_batch(self):
        with tempfile.TemporaryDirectory() as directory:
            cfg = AppConfig(use_source_registry=True)
            output = Path(directory) / "pdf"
            papers = [
                Paper(title="First", pdf_url="https://blocked.example.org/a.pdf",
                      extra_fields={"MCS Ref No": "1"}),
                Paper(title="Second", pdf_url="https://blocked.example.org/b.pdf",
                      extra_fields={"MCS Ref No": "2"}),
            ]
            def fake_fetch(url, path, **kwargs):
                kwargs["status_callback"]("rate_limited")
                return False
            with (
                patch("crawly_core.downloader._fetch_and_verify_pdf", side_effect=fake_fetch) as fetch,
                patch("crawly_core.downloader.resolve_paper_metadata"),
                patch("crawly_core.downloader._candidate_unpaywall_urls", return_value=[]),
                patch("crawly_core.downloader._candidate_europe_pmc_urls", return_value=[]),
            ):
                stats = download_batch_pdfs(papers, output, config=cfg)
            self.assertEqual(stats["missing"], 2)
            self.assertEqual(fetch.call_count, 1)
            registry = SourceRegistry(output / "sources.sqlite")
            self.assertEqual(registry.list_sources("1")[0].last_outcome, "rate_limited")
            self.assertEqual(registry.list_sources("2")[0].attempts, 0)

    def test_cli_explicit_registry_flags_reach_download_config(self):
        from crawly_core.cli import main

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "input.csv"
            db = Path(directory) / "records.sqlite"
            save_papers_to_csv([Paper(title="Test")], path)
            with (
                patch("crawly_core.cli.AppConfig.load", return_value=AppConfig(ai_provider="extractive")),
                patch("crawly_core.cli.render_header"),
                patch("crawly_core.cli.download_batch_pdfs",
                      return_value={"total": 1, "downloaded": 0, "missing": 1}) as batch,
            ):
                self.assertEqual(main([
                    "download", str(path), "-o", str(Path(directory) / "output"),
                    "--source-registry", "--registry-db", str(db),
                ]), 0)
                config = batch.call_args.kwargs["config"]
                self.assertTrue(config.use_source_registry)
                self.assertEqual(config.source_registry_path, str(db))

    def test_transport_success_with_no_callback_will_not_claim_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = SourceRegistry(Path(directory) / "sources.sqlite")
            paper = Paper(title="Title", pdf_url="https://example.edu/doc.pdf",
                          extra_fields={"MCS Ref No": "10"})
            with patch("crawly_core.downloader._fetch_and_verify_pdf", return_value=True):
                result = download_paper_pdf(paper, Path(directory), AppConfig(), registry=registry)
            self.assertTrue(result[0])
            source = registry.list_sources("10")[0]
            self.assertEqual(source.last_outcome, "pdf_received")
            self.assertEqual(source.verified_downloads, 0)

    def test_non_literature_is_skipped_and_no_sources_is_handled(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = SourceRegistry(Path(directory) / "sources.sqlite")
            cfg = AppConfig()
            excluded = Paper(title="Datasheet",
                             extra_fields={"Document Type": "DATASHEET"})
            skipped = download_paper_pdf(excluded, Path(directory), cfg, registry=registry)
            self.assertEqual(skipped[1], "Excluded document type")
            stats = download_batch_pdfs([excluded], Path(directory) / "skip", config=AppConfig(
                use_source_registry=True,
            ))
            self.assertEqual(stats["skipped"], 1)
            self.assertEqual(stats["missing"], 0)
            self.assertEqual(stats["downloaded"], 0)
            self.assertEqual(stats["total"], 1)

            empty = Paper(title="No source", url="NA",
                          extra_fields={"MCS Ref No": "500"})
            with (
                patch("crawly_core.downloader.resolve_paper_metadata"),
                patch("crawly_core.downloader._candidate_europe_pmc_urls", return_value=[]),
            ):
                missing = download_paper_pdf(empty, Path(directory), cfg, registry=registry)
            self.assertEqual(missing[1], "Not Found")

    def test_scihub_fallback_only_if_explicitly_enabled(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = SourceRegistry(Path(directory) / "sources.sqlite")
            paper = Paper(title="P", extra_fields={"MCS Ref No": "600"})
            cfg = AppConfig(use_scihub_fallback=True, scihub_mirrors=["https://sh.example"])
            with (
                patch("crawly_core.downloader.resolve_paper_metadata"),
                patch("crawly_core.downloader._candidate_europe_pmc_urls", return_value=[]),
                patch("crawly_core.downloader._try_scihub_download", return_value="Manual approved mirror") as mock_mirror,
            ):
                done = download_paper_pdf(
                    paper, Path(directory), cfg, custom_scihub_url="https://custom.example",
                    registry=registry,
                )
            self.assertTrue(done[0])
            mock_mirror.assert_called_once()
            self.assertEqual(paper.download_source, "Manual approved mirror")


if __name__ == "__main__":
    unittest.main()
