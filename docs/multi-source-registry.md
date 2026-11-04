# Multi-source candidate registry (experimental)

This is an **opt-in Python API**, not yet a replacement for `crawly download`.
It does not contact any academic site, nor does it bypass authentication.

## CSV interoperability

Crawly's existing columns remain unchanged, including `Abstract` and
`Local PDF`. The reference-metadata change in PR #1 preserves additional
columns such as:

| Column | Meaning |
| --- | --- |
| `MCS Ref No` | Stable bibliography identifier; may be any string ID |
| `MCS Sections` | All citing sections, such as `1; 3.2.1; 4.3` |
| `Expected Filename` | Stable filename for the acquired PDF |
| `Source URL 2`, `Source URL 3` | Alternate possible PDF or landing-page URLs |
| `Access Confidence 1` | Heuristic likelihood of retrieving a PDF at `PDF URL` |
| `Identity Confidence 1` | Heuristic that the PDF matches the reference |
| `Expected Cost 1` | Positive relative retrieval cost |
| `Access Confidence 2` etc. | Corresponding source-specific estimates |
| `Document Type` / `Reference Type` | Optional classification |

The numbered primary `PDF URL` is source 1. Alternates use
`Source URL N` or `PDF URL N`. A distinct `Url of paper` is recorded
as a landing-page candidate. Identical URLs are deduplicated per paper.

Software repositories, source-code records, datasheets, and datasets
explicitly classified in these columns do not generate download candidates.
Books, chapters, and standards are not excluded, but the low-effort policy
for them is **not implemented yet**.

## Python usage

```python
from crawly_core.models import load_papers_from_csv
from crawly_core.source_registry import SourceRegistry

papers = load_papers_from_csv("mcs14_crawly_20_paper_enriched.csv")
registry = SourceRegistry("crawly_sources.sqlite")
for paper in papers:
    registry.import_paper(paper)

for candidate in registry.list_sources("753"):
    print(candidate.url, candidate.priority_score)

# Record outcomes only after an actual download/verification attempt.
registry.record_attempt(
    "753", "https://example.org/vestal.pdf", "verified"
)
```

The last line is illustrative; the source URL must first have been registered.
Current `crawly download` does **not** yet automatically record outcomes
into this registry.

## What confidence means

Two values are kept separate:

- **Access confidence**: a heuristic for obtaining PDF bytes from the current environment.
- **Identity confidence**: a heuristic that those bytes are the correct referenced work.

Both lie in the interval 0–1. A priority score is calculated from their
product, divided by a positive relative cost. The two confidences are
smoothed using two pseudo-observations and the recorded outcomes:
`pdf_received`, `verified`, and `identity_mismatch` count as PDF
responses, while **only** `verified` counts as a confirmed identity.

**These are not statistically calibrated probabilities** and are not a
guarantee of successful acquisition. Scores must not be treated as such
until a calibration exercise has measured success on held-out sources.

A successful PDF signature check alone is not enough to mark a source
`verified`; document identity should also be checked. The registry
distinguishes `authentication_required`, `access_denied`,
`rate_limited`, `not_found`, `download_error`, `pdf_received`,
`identity_mismatch`, and `verified`.

## Persistence and work to follow

The registry uses SQLite with one source row per (reference, URL) pair
and records outcomes and timestamps without discarding earlier attempts.
Separate DB connections prepare for a future host-aware acquisition scheduler.

Not yet implemented: source calibration, dynamic author/institution discovery,
rate-limit-aware worker pools, automatic registry integration with the
downloader, and document identity verification.

The fixed **20-paper MCS set** remains our acceptance benchmark for later
end-to-end acquisition testing. Current unit tests cover registry behavior
offline; they do **not** demonstrate live download success.
