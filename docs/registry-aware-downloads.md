# Registry-aware downloads (opt-in)

This feature is **experimental** and disabled by default. It consumes the
source registry from PR #2 and the CSV-preservation model from PR #1.
Ordinary `crawly download` remains unchanged.

## CLI

```bash
# Use a copy: the download command updates its CSV with local PDF paths.
cp benchmarks/mcs14_pilot20.csv /tmp/mcs14-pilot-run.csv

# Source-annotated CSVs may contain PDF URL and Source URL 2, 3, ...
crawly download /tmp/mcs14-pilot-run.csv \
    -o /tmp/crawly-pdfs \
    --source-registry \
    --registry-db /tmp/crawly-pdfs/source-registry.sqlite
```

Or add to the user-specific `crawly.json`:

```json
{
  "use_source_registry": true,
  "source_registry_path": ""
}
```

A blank database path saves `sources.sqlite` in the output directory.
Keep credentials and machine-specific paths out of committed `config.json`.

## Acquisition flow

1. Respect explicitly classified non-literature references (software,
   software repositories, source code, datasets and datasheets).
2. Use a previously cached PDF when available.
3. Import candidate links from `PDF URL`, `Source URL N`, and
   `Url of paper` into SQLite.
4. Rank them using the heuristic score; attempt each URL only once per
   paper in that invocation.
5. If unsuccessful, ask Crawly's existing OpenAlex/Crossref metadata
   resolver and OA providers for additional candidates, and try them.
6. Record the attempted URL and outcome in SQLite. A 401/403/429
   suppresses further requests to the same host **in that batch only**.
7. Write the existing CSV fields, including reference numbers, all
   citing sections, filenames, scholarly abstracts, and AI notes.

Outcomes include `not_found`, `rate_limited`,
`authentication_required`, `access_denied`, `download_error`,
and `pdf_received`. A byte-valid PDF is **not** proof that it is
the referenced paper, so it is recorded as `pdf_received`, not
`verified`. Identity checking by title/author/DOI is deferred to
another milestone.

This version remains **sequential**. It is not yet an implementation
of per-host request budgets, cross-run cooldowns, dynamic institution
or author discovery, or calibrated download probability estimates.
Existing official API policies and publisher access rules still apply.

### Benchmarking

The cold-start 20-paper dataset in `benchmarks/mcs14_pilot20.csv`
contains no pre-discovered URLs. This feature alone does not yet
guarantee a higher retrieval rate on that dataset: building an actual
discovery pipeline remains a separate goal. Do not represent unit-test
success as a live 20-paper acquisition result.
