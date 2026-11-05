# Bibliographic PDF identity gate — opt-in

The previous registry-aware downloader checks transport/PDF bytes but cannot
claim a downloaded file is the referenced paper. This experimental gate adds
a **conservative, offline, evidence-retaining** first-page bibliographic check.

## Run

```bash
crawly download /path/to/enriched-pilot.csv \
  -o /path/to/verified-pdfs \
  --verify-identity \
  --registry-db /path/to/verified-pdfs/sources.sqlite
```

`--verify-identity` implies the persistent source registry. Without it,
existing downloads continue to operate unchanged. You may also configure
`"verify_pdf_identity": true` in private `crawly.json`.

### Identity decision rules

- `verified`: normalized full title appears on page one, first author's
  surname appears in that page or PDF author metadata, and expected year or
  DOI appears (or the reference has no known year). No explicit contradicting
  header DOI or strongly conflicting title metadata.
- `needs_review`: text is missing/unextractable, or evidence is incomplete.
- `mismatch`: contradictory DOI in the first-page DOI field or a clearly
  different embedded title also appearing as the PDF heading.

These are intentionally conservative **heuristic decisions**, not a proof
of identity and not a substitute for manual audit of high-impact references.
False negatives can occur with title typography, scanned documents, different
editions, author naming conventions or inaccessible embedded text. The tool
does not use OCR or request external verification data.

Every source assessment is retained in SQLite `identity_checks` alongside
the source attempt; the final decision and evidence JSON are preserved in
`PDF Identity Status` and `PDF Identity Evidence` CSV columns.

A file with status `mismatch` or `needs_review` is moved under
`identity_review/<status>/` using a source-derived unique name rather than
being counted as an accepted PDF. Crawly continues to other known sources.
The result is counted as downloaded only after `verified`. Previously
cached PDFs are checked too and quarantined if insufficient or mismatched.

Review PDFs are **retained, not deleted**. Inspect evidence before relying
on automated decisions; do not equate a 100% unit-test pass rate with a
successful real-paper verification benchmark.

A reader can inspect saved assessments without making network requests:

```python
from crawly_core.source_registry import SourceRegistry
for evidence in SourceRegistry("verified-pdfs/sources.sqlite").list_identity_checks("753"):
    print(evidence["url"], evidence["status"], evidence["evidence"])
```

## Status

Identity verification is currently opt-in, uses only first-page text and
metadata, and has offline fixture tests. It has **not yet** been validated
against all 20 cold-start MCS references. Strict mode is recommended for
experimental runs with enriched source URLs.
