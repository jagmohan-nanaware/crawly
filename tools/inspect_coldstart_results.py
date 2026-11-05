#!/usr/bin/env python3
"""Offline diagnosis of an MCS cold-start download. Does not fetch anything."""

from __future__ import annotations

import argparse
import csv
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path
from urllib.parse import urlsplit


def summarize(csv_file: Path, database: Path) -> str:
    with csv_file.open(newline="", encoding="utf-8-sig") as handle:
        papers = list(csv.DictReader(handle))
    registry = defaultdict(list)
    if database.is_file():
        with sqlite3.connect(database) as connection:
            exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='sources'"
            ).fetchone()
            if exists:
                for ref, url, attempts, outcome, response_count in connection.execute(
                    "SELECT reference_id,url,attempts,last_outcome,pdf_responses FROM sources"
                ):
                    registry[str(ref)].append(
                        (url, attempts, outcome, response_count)
                    )
    events = defaultdict(list)
    if database.is_file():
        with sqlite3.connect(database) as connection:
            has_events = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='discovery_events'"
            ).fetchone()
            if has_events:
                for ref, provider, outcome, detail in connection.execute(
                    "SELECT reference_id, provider, outcome, detail FROM discovery_events ORDER BY id"
                ):
                    events[str(ref)].append((provider, outcome, detail))
    counts = Counter()
    provider_outcomes = Counter()
    lines = [
        "CRAWLY COLD-START DIAGNOSTIC (existing CSV + SQLite only)",
        f"Input: {csv_file}",
        f"Registry: {database} ({'present' if database.is_file() else 'missing'})",
        "",
    ]
    for p in papers:
        ref = p.get("MCS Ref No", "").strip() or p.get("DOI", "").strip()
        doi = (p.get("DOI") or "").strip()
        url = (p.get("PDF URL") or "").strip()
        local = (p.get("Local PDF") or "").strip()
        status = (p.get("PDF Identity Status") or "").strip()
        candidates = registry.get(ref, [])
        tried = [c for c in candidates if c[1]]
        outcomes = ", ".join(
            f"{urlsplit(c[0]).hostname or '?'}:{c[2] or 'unattempted'}({c[1]})"
            for c in tried
        )
        counts["references"] += 1
        counts["doi"] += bool(doi and doi.lower() != "na")
        counts["pdf_url"] += bool(url and url.lower() != "na")
        counts["registry_candidates"] += bool(candidates)
        counts["attempted"] += bool(tried)
        counts["downloaded"] += bool(local and Path(local).is_file())
        counts["reported_local"] += bool(local)
        counts["no_candidates"] += not bool(candidates)
        counts["candidates_not_attempted"] += bool(candidates and not tried)
        counts["api_or_download_failure"] += bool(tried and not local)
        lines.append(
            f"#{ref or '?':>3} | DOI={'yes' if doi else 'no':3} "
            f"| CSV PDF URL={'yes' if url else 'no':3} "
            f"| candidates={len(candidates):2} tried={len(tried):2} "
            f"| local={'yes' if local else 'no':3} "
            f"| {status or '-'}"
        )
        if outcomes:
            lines.append(f"      attempts: {outcomes}")
        for provider, outcome, detail in events.get(ref, []):
            provider_outcomes[(provider, outcome)] += 1
            lines.append(f"      discovery: {provider}={outcome} ({detail})")
    lines.extend([
        "",
        "SUMMARY",
        *[f"  {name}: {counts[name]}/{len(papers)}" for name in [
            "doi", "pdf_url", "registry_candidates", "attempted",
            "reported_local", "downloaded", "no_candidates",
            "candidates_not_attempted", "api_or_download_failure",
        ]],
        "",
        "Interpretation: 0 candidates suggests discovery failure; candidates with",
        "failed attempts suggest access, rate-limit, link or PDF-response failure.",
        "Discovery-stage HTTP outcomes are recorded for new OpenAlex and",
        "Semantic Scholar probes. Legacy metadata resolver, Unpaywall,",
        "Crossref and Europe PMC do not yet have complete API status telemetry.",
        "",
        "DISCOVERY PROVIDER OUTCOMES",
        *[f"  {provider}: {status} = {count}" for (provider, status), count in sorted(provider_outcomes.items())],
    ])
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results", type=Path, required=True)
    parser.add_argument("--database", type=Path, required=True)
    args = parser.parse_args()
    print(summarize(args.results, args.database))


if __name__ == "__main__":
    main()
