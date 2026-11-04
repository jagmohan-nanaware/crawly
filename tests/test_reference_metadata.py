"""Regression tests for scholarly-reference CSV metadata and safe filenames."""

import csv
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from crawly_core.models import Paper, load_papers_from_csv, save_papers_to_csv
from crawly_core.downloader import download_batch_pdfs


class TestReferenceMetadataPreservation(unittest.TestCase):
    def test_multi_section_metadata_abstract_and_sources_survive_roundtrip(self):
        with tempfile.TemporaryDirectory() as directory:
            original = Path(directory) / "mcs_pilot.csv"
            original.write_text(
                'Paper Title,Year,Author,Abstract,PDF URL,MCS Ref No,MCS Sections,'
                'Expected Filename,Source URL 2,Source URL 3\n'
                '"Mixed-Criticality Systems",2024,"Burns, Davis",'
                '"Original scholarly abstract",https://example.org/one.pdf,'
                '158,"2; 3.2.1; 4.3; 5.2",'
                '158-2+3.2.1+4.3+5.2-Burns-14.pdf,'
                'https://repository.example.org/two.pdf,'
                'https://author.example.org/three.pdf\n',
                encoding="utf-8",
            )
            papers = load_papers_from_csv(original)
            self.assertEqual(len(papers), 1)
            paper = papers[0]
            self.assertEqual(paper.abstract, "Original scholarly abstract")
            self.assertEqual(paper.extra_fields["MCS Ref No"], "158")
            self.assertEqual(paper.extra_fields["MCS Sections"], "2; 3.2.1; 4.3; 5.2")
            self.assertEqual(paper.extra_fields["Source URL 2"], "https://repository.example.org/two.pdf")
            self.assertEqual(paper.safe_filename(), "158-2+3.2.1+4.3+5.2-Burns-14.pdf")

            paper.local_pdf = "/tmp/verified.pdf"
            paper.download_source = "Institutional Repository"
            save_papers_to_csv(papers, original)
            again = load_papers_from_csv(original)[0]
            self.assertEqual(again.extra_fields, paper.extra_fields)
            self.assertEqual(again.abstract, paper.abstract)
            self.assertEqual(again.local_pdf, "/tmp/verified.pdf")
            self.assertEqual(again.download_source, "Institutional Repository")
            self.assertEqual(again.safe_filename(), paper.safe_filename())
            with original.open(encoding="utf-8", newline="") as handle:
                fields = csv.DictReader(handle).fieldnames
            self.assertIn("MCS Ref No", fields)
            self.assertIn("Source URL 3", fields)

    def test_filename_prefers_reference_identifier_to_avoid_title_collisions(self):
        title = "A common title"
        first = Paper(title=title, extra_fields={"Expected Filename": "94-3.3+7.2-Baruah-14.pdf"})
        second = Paper(title=title, extra_fields={"Expected Filename": "99-3.2.1+4.2+6-Baruah-14.pdf"})
        self.assertNotEqual(first.safe_filename(), second.safe_filename())
        self.assertEqual(Paper(title=title).safe_filename(), "A common title.pdf")

    def test_supplied_filename_cannot_escape_output_directory(self):
        paper = Paper(title="Title", extra_fields={"Expected Filename": "../../dangerous/owned.pdf"})
        self.assertEqual(paper.safe_filename(), "owned.pdf")
        paper.extra_fields["Expected Filename"] = "..\\..\\dangerous\\owned.pdf"
        self.assertEqual(paper.safe_filename(), "owned.pdf")

    def test_regular_crawly_csv_remains_compatible(self):
        with tempfile.TemporaryDirectory() as directory:
            csv_path = Path(directory) / "ordinary.csv"
            original = Paper(title="Original", abstract="Database abstract", pdf_url="https://example.org/p.pdf")
            save_papers_to_csv([original], csv_path)
            reread = load_papers_from_csv(csv_path)[0]
            self.assertEqual(reread.extra_fields, {})
            self.assertEqual(reread.abstract, "Database abstract")
            self.assertEqual(reread.safe_filename(), "Original.pdf")

    def test_download_batch_does_not_erase_imported_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "batch.csv"
            paper = Paper(
                title="Reference paper",
                abstract="Publisher abstract",
                extra_fields={
                    "MCS Ref No": "753",
                    "MCS Sections": "1; 2; 3; 9",
                    "Expected Filename": "753-1+2+3+9-Vestal-14.pdf",
                },
            )
            with patch("crawly_core.downloader.download_paper_pdf", return_value=(False, "Not Found", None)):
                result = download_batch_pdfs([paper], directory, csv_path=source)
            self.assertEqual(result["missing"], 1)
            reloaded = load_papers_from_csv(source)[0]
            self.assertEqual(reloaded.extra_fields, paper.extra_fields)
            self.assertEqual(reloaded.abstract, "Publisher abstract")
            self.assertEqual(reloaded.safe_filename(), paper.safe_filename())


if __name__ == "__main__":
    unittest.main()
