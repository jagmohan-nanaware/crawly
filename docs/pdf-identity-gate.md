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

- `verified`: normalized full title and first author's surname match,
  with no explicit contradicting DOI/title metadata. The strongest match
  includes publication year or DOI; an **exact first-page heading title and
  first author** may also establish document identity if year and DOI are
  absent from the PDF. Evidence records which rule was satisfied.
- `needs_review`: text is missing/unextractable, or evidence is incomplete.
- `mismatch`: contradictory DOI in the first-page DOI field or a clearly
  different embedded title also appearing as the PDF heading.

These are intentionally conservative **heuristic decisions**, not a proof
of identity and not a substitute for manual audit of high-impact references.
A result with reason `exact_header_title_and_author_only` verifies
*document identity provisionally*; it does **not** independently confirm
publication year, venue, or DOI. Keep those fields subject to bibliographic
metadata validation.
False negatives can occur with title typography, scanned documents, different
editions, author naming conventions or inaccessible embedded text. The tool
does not use OCR or request external verification data.

Every source assessment is retained in SQLite `identity_checks` alongside
the source attempt. The CSV preserves `PDF Identity Status`,
`PDF Identity Evidence`, and a human-readable `PDF Review Reason`.

**A downloadable `needs_review` PDF is retained and counted as retrieved.**
Its `Local PDF` column points to the saved candidate under
`identity_review/needs_review/`; it is not falsely counted as
bibliographically verified. Crawly continues trying other sources for a
verified copy, preferring one if available. The best reviewable candidate
remains available if none can be verified. A cache hit receives the same
review status rather than triggering needless repeat downloads.

A confirmed `mismatch` is also retained in `identity_review/mismatch/`
for inspection, but **is not accepted as that reference's PDF**.
A source-derived filename prevents overwriting earlier evidence.

The CLI reports `downloaded` (including reviewable candidates),
`verified`, `needs_review`, `identity_mismatch`, and `missing`
independently. Never interpret `downloaded` as `verified`.

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

## Reproducible enriched three-paper smoke test

The tracked `benchmarks/mcs14_smoke3_enriched.csv` is deliberately
**source-enriched**, unlike the separate 20-paper cold-start benchmark.
It contains original MCS reference numbers and candidate URLs already
collected for references #34, #158 and #753. The verification run must
work from a copy, because Crawly updates the input CSV:

```bash
RUN="$HOME/scratch/crawly-data/identity-smoke3"
mkdir -p "$RUN/pdfs"
cp benchmarks/mcs14_smoke3_enriched.csv "$RUN/input.csv"
crawly download "$RUN/input.csv" -o "$RUN/pdfs" \
    --verify-identity --registry-db "$RUN/sources.sqlite"
```

Inspect `PDF Identity Status` and `PDF Identity Evidence` in the output
CSV and `identity_checks` in SQLite, and review PDFs left in
`$RUN/pdfs/identity_review/`. An outcome of `needs_review` can reflect a
perfectly correct paper with incomplete extractable metadata. Do not
treat a 3/3 byte-retrieval outcome as 3/3 bibliographically verified.
