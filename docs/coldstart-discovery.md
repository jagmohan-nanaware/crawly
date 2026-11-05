# Cold-start OA discovery v2 — source candidates, not blind URLs

## Baseline (20-paper fixed MCS pilot, 2026-11-05)

The first URL-free, cold-start run found one PDF (reference #161).
The offline diagnostic found:
- 17/20 records resolved a DOI.
- 15/20 records had **no PDF candidate URL at all**.
- Five records had a candidate; four attempts failed and one succeeded.

These counts reflect **Crawly v1 discovery**, not global availability of
these papers. Do not conflate `Not Found` with paywall or lack of OA copies.

## Discovery changes

1. Keep the earlier DOI resolver as a compatible first step.
2. Query OpenAlex by DOI **and fall back to title search if the DOI lookup
   fails or gives an unrelated result**.
3. Check returned title, plausible publication year and first author
   before registering sources; do not accept a plausible title alone if
   author/year evidence clearly contradicts the reference.
4. Collect *all* OA locations, including accepted manuscripts and repository
   copies, rather than only `best_oa_location`.
5. Independently query Semantic Scholar by paper title; collect public
   `openAccessPdf` URLs and linked arXiv preprints for matching works.
6. Preserve source provenance, candidate URL, and attempts in SQLite.
7. Record API successes, empty matches, HTTP 401/403/404/429/5xx and
   request/JSON errors in the `discovery_events` table.

Important: a source URL is not evidence that a PDF is downloadable. A
received PDF is not proof of paper identity. Registry source attempts and
the optional `--verify-identity` gate remain the acceptance safeguards.

These probes use published APIs and do not attempt to bypass access
restrictions. If a provider rate-limits or is inaccessible, the diagnostic
says so rather than fabricating source candidates.

## Reproducible experimental comparison

Keep the original `coldstart20-v1` directory unchanged. Run on a **fresh
copy of the same frozen input CSV**:

```bash
RUN="$HOME/scratch/crawly-data/coldstart20-v2"
mkdir -p "$RUN/pdfs"
cp benchmarks/mcs14_pilot20.csv "$RUN/input.csv"
python3 crawly.py download "$RUN/input.csv" -o "$RUN/pdfs" \
  --verify-identity --registry-db "$RUN/sources.sqlite"
python3 tools/inspect_coldstart_results.py \
  --results "$RUN/input.csv" --database "$RUN/sources.sqlite" \
  | tee "$RUN/diagnostics.txt"
```

Compare `v1` vs `v2` using downloaded, verified, needs-review,
mismatch and missing. Also inspect provider statuses for the records
that still lack candidates. A successful enriched three-paper smoke test
does not imply corresponding cold-start discovery success.

Only introduce concurrency and full 844-reference execution after a
meaningful, reproducible improvement on this fixed benchmark.


## v2 result and diagnosed bottleneck

The 20-paper v2 run still retrieved **1/20**. The enhanced diagnostic
identified **20/20 Semantic Scholar requests returning HTTP 429**;
OpenAlex matched 15 references, returned 429 for four, and did not
produce a candidate for most matched references. This is not evidence
that the referenced PDFs do not exist.

## Respecting provider limits (v3 changes)

- First HTTP 429 from either OpenAlex or Semantic Scholar disables
  additional requests to that provider for the remainder of the batch.
  Each skipped lookup is recorded as `skipped_rate_limited`.
  Stop the batch and resume later if a daily quota is exhausted; do
  **not** rotate IPs or API keys to bypass provider limits.
- The registry download path no longer makes duplicated OpenAlex
  requests via the older DOI resolver.
- The current Crossref lookup still resolves missing DOIs. Unpaywall
  and Europe PMC results are still attempted if appropriate.
- The optional existing `semantic_scholar_api_key` config field is
  now sent as the documented `x-api-key` header for title searches.
- The optional `OPENALEX_API_KEY` environment variable authorizes
  OpenAlex queries. No secret is included in stored URL candidates,
  discovery events or output CSV.
- For a confirmed open-access work with `has_content.pdf` and an
  OpenAlex `content_urls.pdf` link, Crawly registers the cached
  OpenAlex PDF **only when** `OPENALEX_API_KEY` is set. The key is
  passed at HTTP request time, and only to the exact
  `https://content.openalex.org/works/` endpoint prefix.

OpenAlex's free API key is available from the OpenAlex website.
Never commit keys to Git or paste them into an issue. In a shell,
enter a key without putting it in shell history:

```bash
read -rs -p 'OpenAlex API key: ' OPENALEX_API_KEY
printf '\\n'
export OPENALEX_API_KEY
```

To use a Semantic Scholar key, set `semantic_scholar_api_key` in the
ignored local configuration file (never commit this file).

## Next comparison

Use a clean run directory and preserve v1 and v2:
```bash
RUN="$HOME/scratch/crawly-data/coldstart20-v3"
mkdir -p "$RUN/pdfs"
cp benchmarks/mcs14_pilot20.csv "$RUN/input.csv"
python3 crawly.py download "$RUN/input.csv" \\
  -o "$RUN/pdfs" --verify-identity \\
  --registry-db "$RUN/sources.sqlite"
python3 tools/inspect_coldstart_results.py \\
  --results "$RUN/input.csv" --database "$RUN/sources.sqlite" \\
  | tee "$RUN/diagnostics.txt"
```

Evaluate source candidates and API statuses first, then PDF retrieval,
PDF identity and review outcomes. Never classify papers that could not
be discovered during HTTP 429 as globally unavailable.
