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
