# Repository and author-hosted PDF source discovery

## Recorded first live result: three URL-free papers

The office-machine `coldstart3-repo-v1` run (2026-11-05) reported:

| MCS ref | Matched providers | Registered PDF candidates | Acquired |
| --- | --- | --- | --- |
| 34 — Anderson et al. | none (OpenAlex, HAL, OpenAIRE) | 0 | missing |
| 158 — Burns & Davis | OpenAlex (no URL); **OpenAIRE** | 1 | **verified**, York-hosted PDF |
| 753 — Vestal | OpenAlex and OpenAIRE (no URL) | 0 | missing |

The single downloaded candidate originated from OpenAIRE and the download
source was `www-users.cs.york.ac.uk`. This is **1/3 verified cold-start**,
versus **0/3** in the immediately preceding authenticated OpenAlex
three-paper experiment. The fixed full 20-paper baseline remains **1/20**;
it has not yet been retested with repository discovery enabled.

HAL matched none of the three papers. Semantic Scholar returned HTTP 429
for the first request; subsequent searches were correctly skipped.

**Interpretation:** The new OpenAIRE fallback has proven at least one
real cold-start acquisition, but neither of the remaining two missing
references was resolved. The missing records are not evidence that those
papers are unavailable; both were retrieved in the earlier enriched-URL
smoke test. Do not inflate this result by manually adding those known
links to the cold-start input.

**Next decision:** the remaining discovery problem is likely coverage of
individual author-hosted/publicly indexed files. The optional,
key-dependent general web search route can be evaluated independently
against the two unresolved papers. No evidence yet supports another
whole-bibliography run.


## Problem

The fixed 20-paper mixed-criticality benchmark downloaded **1/20** papers
from bibliography-only input. Authenticated OpenAlex was able to identify
some publications but not their public author PDFs. We have separately
downloaded references #34, #158 and #753 from known enriched URLs; that
does **not** establish a cold-start discovery success rate.

This experimental layer improves **candidate discovery**, not PDF identity.

## How the fallback works

The existing registry tries the input's known URLs, then OpenAlex /
Semantic Scholar, Unpaywall and Europe PMC. Only after those sources fail
does Crawly use these independent, documented discovery interfaces:

| Provider | Enabled | What is registered |
| --- | --- | --- |
| HAL | Default | Main file URL (`fileMain_s`), plus HAL landing page |
| OpenAIRE Graph v4 | Default | URLs of publication instances explicitly marked Open Access |
| Brave Search API | **Opt-in** | Full-title-matched scholarly web search hits for `filetype:pdf` |

The returned records must match title, publication year (when available)
and first-author surname (when available). Search-result titles/snippets
are scored for matching title words, but remain **unverified leads**.
Unrelated search hits are rejected.

Candidate provenance is retained in SQLite `sources`; API successes,
HTTP errors, rate limiting and no-match outcomes are logged to
`discovery_events`. The existing PDF-byte and optional bibliographic
identity verification check every retrieved candidate. Uncertain PDFs
remain available as `needs_review` with reasons; confirmed mismatches
do not count as the reference's paper.

The provider-specific HTTP 429 stop rule remains in force. This layer
does not bypass institutional access policies or scrape Google Scholar.

### Optional Brave web-index discovery

**HAL and OpenAIRE do not require keys for the endpoints used.** They may
still miss personal author websites such as university or MPI-SWS pages.
For those, a general search index is the more natural tool.

The Brave Search API is a third-party **commercial service**, with its
own signup, credits and usage charges. This is optional and Crawly does
not make Brave calls unless the user sets `BRAVE_SEARCH_API_KEY`.
See the provider's current plan and terms before enabling it.
Never put this key in `crawly.json`, Git commits, logs, or chat messages.

Use secure shell input:

```bash
read -rs -p 'Brave Search API key: ' BRAVE_SEARCH_API_KEY
printf '\\n'
export BRAVE_SEARCH_API_KEY
```

Your already configured `OPENALEX_API_KEY` remains separate.

## Experimental rollout

**First, use the known three references with no prefilled URLs**,
in a fresh directory so the cold-start comparison is meaningful.

```bash
git fetch origin
git switch --track origin/feature/repository-author-discovery

RUN="$HOME/scratch/crawly-data/coldstart3-repositories-v1"
mkdir -p "$RUN/pdfs"
cp benchmarks/mcs14_coldstart3_no_urls.csv "$RUN/input.csv"

python3 crawly.py download "$RUN/input.csv" \
  -o "$RUN/pdfs" --verify-identity \
  --registry-db "$RUN/sources.sqlite"

python3 tools/inspect_coldstart_results.py \
  --results "$RUN/input.csv" --database "$RUN/sources.sqlite" \
  | tee "$RUN/diagnostics.txt"
```

If the no-key run still does not locate #34, #158 and #753, examine
OpenAIRE/HAL provider statuses, then decide whether the optional general
web-index route is worth using. Do not assume the repositories index all
author-hosted PDFs.

Only after a useful three-paper result should we re-run the fixed
20-paper cold-start input. The first two 20-paper runs (1/20) remain
the unchanged baseline. Do not run all 844 references yet.

### Validation

Offline tests use representative HAL Solr JSON, OpenAIRE v4 research
product JSON, and Brave Web Search result shapes. They are deterministic.
A successful unit-test run does **not** prove that any third-party
repository currently indexes a specific MCS PDF.
