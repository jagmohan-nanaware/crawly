"""Frozen 20-paper benchmark integrity gate (offline; not a download-rate test)."""

import tempfile
import unittest
from pathlib import Path

from crawly_core.models import load_papers_from_csv
from crawly_core.source_registry import SourceRegistry

PILOT_IDS = (
    "1", "2", "3", "4", "34", "57", "94", "99", "113", "158",
    "161", "315", "387", "607", "610", "614", "615", "731", "753", "797",
)


class TestMcs20Benchmark(unittest.TestCase):
    def test_original_pilot_is_stable_and_not_pre_enriched(self):
        sample = Path(__file__).resolve().parents[1] / "benchmarks" / "mcs14_pilot20.csv"
        papers = load_papers_from_csv(sample)
        self.assertEqual(len(papers), 20)
        self.assertEqual(tuple(p.extra_fields["MCS Ref No"] for p in papers), PILOT_IDS)
        self.assertEqual(len({p.safe_filename() for p in papers}), 20)
        by_ref = {p.extra_fields["MCS Ref No"]: p for p in papers}
        self.assertEqual(
            by_ref["753"].extra_fields["MCS Sections"],
            "1; 2; 3; 3.2.3; 9; 10; 12",
        )
        self.assertEqual(
            by_ref["158"].extra_fields["MCS Sections"],
            "2; 3.2.1; 4.3; 5.2",
        )
        self.assertEqual(by_ref["607"].year, 2014)
        self.assertTrue(all(p.authors and p.title for p in papers))
        self.assertTrue(all(not p.pdf_url and not p.local_pdf for p in papers))
        with tempfile.TemporaryDirectory() as directory:
            registry = SourceRegistry(Path(directory) / "source_registry.sqlite")
            self.assertTrue(all(registry.import_paper(p) == 0 for p in papers))
            self.assertEqual(registry.list_sources("753"), [])


if __name__ == "__main__":
    unittest.main()
