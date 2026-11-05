"""Cold-start open-access source discovery with provenance and API diagnostics.

This module collects *candidate* URLs; only the downloader can validate PDF
bytes and then screen a paper's identity. Never use title search alone to
declare a paper verified.
"""

from __future__ import annotations

import difflib
import re
import unicodedata
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

import requests

from .models import AppConfig, DEFAULT_API_ENDPOINTS, Paper
from .source_registry import CandidateSource, SourceRegistry
from .sources import get_polite_headers, normalize_doi


def _clean(value: str) -> str:
    value = unicodedata.normalize("NFKD", str(value or ""))
    value = re.sub(r"[‐‑‒–—―]", " ", value)
    value = value.encode("ascii", "ignore").decode("ascii")
    return " ".join(re.findall(r"[a-z0-9]+", value.lower()))


def _matching_paper(paper: Paper, item: Dict[str, Any], service: str) -> bool:
    """Require a convincing title, plausible year and noncontradictory author."""
    expected = _clean(paper.clean_title())
    found = _clean(item.get("title") or item.get("display_name") or "")
    if not expected or not found:
        return False
    ratio = difflib.SequenceMatcher(None, expected, found).ratio()
    if ratio < 0.83:
        return False
    try:
        target_year = int(paper.year or 0)
        found_year = int(item.get("publication_year" if service == "openalex" else "year") or 0)
    except (ValueError, TypeError):
        return False
    if target_year and found_year and abs(target_year - found_year) > 1:
        return False
    first_author = re.split(r"\s+and\s+|\s*,\s*|\s*;\s*", str(paper.authors or ""), maxsplit=1, flags=re.I)[0]
    surname = _clean(first_author).split()
    if surname:
        family = surname[-1]
        authors = item.get("authorships") if service == "openalex" else item.get("authors")
        authors = authors or []
        if authors:
            first = authors[0]
            name = (first.get("author") or {}).get("display_name", "") if service == "openalex" else first.get("name", "")
            if family not in _clean(name).split():
                return False
    return True


def _get_json(
    registry: SourceRegistry,
    reference_id: str,
    provider: str,
    url: str,
    params: Optional[dict],
    cfg: AppConfig,
) -> Optional[dict]:
    """Log provider failures explicitly; never store credentials or query tokens."""
    try:
        response = requests.get(
            url, params=params, headers=get_polite_headers(cfg),
            timeout=cfg.request_timeout,
        )
        if response.status_code != 200:
            status = {
                401: "authentication_required", 403: "access_denied",
                404: "not_found", 429: "rate_limited",
            }.get(response.status_code, "http_error")
            registry.record_discovery_event(
                reference_id, provider, status,
                f"HTTP {response.status_code}",
            )
            return None
        payload = response.json()
        if not isinstance(payload, dict):
            registry.record_discovery_event(reference_id, provider, "invalid_response", "Non-object JSON")
            return None
        return payload
    except (requests.RequestException, ValueError) as error:
        registry.record_discovery_event(
            reference_id, provider, "request_error", type(error).__name__,
        )
        return None


def _add_openalex_locations(
    item: Dict[str, Any], ref_id: str,
    collected: List[CandidateSource],
) -> None:
    """Keep independently hosted OA copies, not just best_oa_location."""
    locs = [(item.get("best_oa_location") or {}, True)]
    locs.extend((loc or {}, False) for loc in (item.get("locations") or []))
    locs.append((item.get("primary_location") or {}, False))
    seen = {c.url for c in collected}
    for loc, best in locs:
        if not (best or loc.get("is_oa")):
            continue
        pdf_url = loc.get("pdf_url")
        landing = loc.get("landing_page_url")
        for url, kind in ((pdf_url, "discovered_oa"), (landing, "landing_page")):
            if not isinstance(url, str) or not url.strip() or url in seen:
                continue
            try:
                candidate = CandidateSource(
                    reference_id=ref_id, url=url, source_type=kind,
                    discovered_via="OpenAlex OA",
                    access_confidence=0.8 if kind == "discovered_oa" else 0.35,
                    identity_confidence=0.8,
                    expected_cost=1.0 if kind == "discovered_oa" else 2.0,
                )
            except ValueError:
                continue
            collected.append(candidate)
            seen.add(url)
    oa_url = (item.get("open_access") or {}).get("oa_url")
    if isinstance(oa_url, str) and oa_url and oa_url not in seen:
        try:
            collected.append(CandidateSource(
                reference_id=ref_id, url=oa_url, source_type="landing_page",
                discovered_via="OpenAlex OA",
                access_confidence=0.3, identity_confidence=0.7, expected_cost=2.0,
            ))
        except ValueError:
            pass


def _discover_openalex(
    paper: Paper, cfg: AppConfig, registry: SourceRegistry, ref_id: str,
) -> List[CandidateSource]:
    url = cfg.api_endpoints.get("openalex_works", DEFAULT_API_ENDPOINTS["openalex_works"]).rstrip("/")
    payload = None
    items: List[dict] = []
    if paper.doi:
        endpoint = f"{url}/https://doi.org/{quote(normalize_doi(paper.doi), safe='/')}"
        payload = _get_json(registry, ref_id, "OpenAlex", endpoint, None, cfg)
        if payload and _matching_paper(paper, payload, "openalex"):
            items = [payload]
    # A stale/bad DOI or DOI-lookup failure must not suppress title discovery.
    if not items:
        payload = _get_json(
            registry, ref_id, "OpenAlex", url,
            {"search": paper.clean_title(), "per-page": 5}, cfg,
        )
        items = (payload or {}).get("results") or []
    collected: List[CandidateSource] = []
    matched = 0
    for item in items:
        if not isinstance(item, dict) or not _matching_paper(paper, item, "openalex"):
            continue
        matched += 1
        if not paper.doi:
            paper.doi = normalize_doi(item.get("doi")) or ""
        _add_openalex_locations(item, ref_id, collected)
    if payload is not None:
        registry.record_discovery_event(ref_id, "OpenAlex", "matched" if matched else "no_match",
                                        f"matches={matched}, candidates={len(collected)}")
    return collected


def _discover_semantic_scholar(
    paper: Paper, cfg: AppConfig, registry: SourceRegistry, ref_id: str,
) -> List[CandidateSource]:
    url = cfg.api_endpoints.get(
        "semantic_scholar_search", DEFAULT_API_ENDPOINTS["semantic_scholar_search"]
    )
    payload = _get_json(
        registry, ref_id, "Semantic Scholar", url,
        {"query": paper.clean_title(), "limit": 5,
         "fields": "title,year,authors,openAccessPdf,externalIds"}, cfg,
    )
    if payload is None:
        return []
    collected: List[CandidateSource] = []
    matched = 0
    seen: set[str] = set()
    for item in payload.get("data") or []:
        if not isinstance(item, dict) or not _matching_paper(paper, item, "semantic"):
            continue
        matched += 1
        oa_pdf = (item.get("openAccessPdf") or {}).get("url")
        if isinstance(oa_pdf, str) and oa_pdf and oa_pdf not in seen:
            try:
                collected.append(CandidateSource(
                    ref_id, oa_pdf, source_type="discovered_oa",
                    discovered_via="Semantic Scholar OA",
                    access_confidence=0.75, identity_confidence=0.8,
                ))
                seen.add(oa_pdf)
            except ValueError:
                pass
        preprint = (item.get("externalIds") or {}).get("ArXiv")
        if isinstance(preprint, str) and re.fullmatch(r"[a-zA-Z0-9./-]{3,50}", preprint):
            preprint_url = f"https://arxiv.org/pdf/{preprint}.pdf"
            if preprint_url not in seen:
                collected.append(CandidateSource(
                    ref_id, preprint_url, source_type="discovered_oa",
                    discovered_via="Semantic Scholar arXiv",
                    access_confidence=0.75, identity_confidence=0.75,
                ))
                seen.add(preprint_url)
    registry.record_discovery_event(
        ref_id, "Semantic Scholar", "matched" if matched else "no_match",
        f"matches={matched}, candidates={len(collected)}",
    )
    return collected


def discover_oa_sources(
    paper: Paper, cfg: AppConfig, registry: SourceRegistry, reference_id: str,
) -> int:
    """Search two independent academic services and persist candidate provenance."""
    candidates = _discover_openalex(paper, cfg, registry, reference_id)
    candidates.extend(_discover_semantic_scholar(paper, cfg, registry, reference_id))
    known = {source.url for source in registry.list_sources(reference_id)}
    added = 0
    for candidate in candidates:
        if candidate.url in known:
            continue
        registry.upsert(candidate)
        known.add(candidate.url)
        added += 1
    return added
