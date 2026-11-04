# Fixed 20-paper MCS acquisition benchmark

The fixed input `mcs14_pilot20.csv` is derived from the 20 references
selected from Burns & Davis, *Mixed Criticality Systems — A Review*,
14th and final edition. This benchmark remains the same across pipeline
versions. Original reference IDs, all citing sections, authors, years and
expected PDF filenames are included.

**Cold-start input:** DOI, direct PDF links and existing local file paths
are intentionally omitted. Otherwise past manual source discovery would
make new engines appear better than they are.

This fixture deliberately contains bibliography entries with multiple
citing sections. `tests/test_benchmark_20.py` verifies row count,
identity, uniqueness of requested filenames, and section mapping offline.
It is **not** a live download performance test.

## Live acceptance criteria (to be implemented)

Evaluate exactly these 20 records on the same machine/network class and
with a recorded config and version. Save for each reference:

- API/source discovery attempts and which candidate URLs were returned;
- HTTP result, retrieval timing, response type and backoff;
- actual PDF path, checksum, and validation of title/authors/DOI;
- document-type policy decisions and whether a manual step was required;
- independently preserved abstract and section mapping;
- number of verified *automated* downloads out of 20, and number
  collected including manual retrieval.

Avoid reporting a valid PDF file as an identity-verified paper without
checking its content. Keep automated success separate from collection
coverage and manual acquisition.

## Historical context, not a verified performance claim

In exploratory runs with Crawly and hand-enriched source URLs,
collection progressed from 2/20 to 12/20, then 14/20, then 15/20 with
a Scholar-discovered source, and 16/20 including one manual acquisition.
These counts were not accompanied by a full independent PDF identity
audit, and the test inputs changed between runs. They are motivating
observations, **not** comparable automated benchmark results.

The initial acceptance target is **at least 16/20 automatically
downloaded, identity-verified papers**, while preserving abstracts and
reference metadata. The target is not yet achieved by a controlled test.
