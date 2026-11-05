"""Repository and author-hosted scholarly PDF candidate discovery.

Supported documented interfaces:
- HAL institutional/author archive Solr JSON search.
- OpenAIRE Graph v4 research-products (OA instance URLs).
- Optional Brave Web Search API (author-hosted and institutional PDF index).

Sources are *candidates*, never accepted PDFs. The existing downloader
validates content and the optional bibliographic identity gate checks it.
No publisher access restrictions, CAPTCHA or paywalls are bypassed.
"""

from __future__ import annotations

import ipaddress
import os
import re
from typing import Any, List, Optional, Sequence
from urllib.parse import urlsplit

from .discovery import _clean, _get_json, _matching_paper
from .models import AppConfig, Paper
from .source_registry import CandidateSource, SourceRegistry

HAL_API = "https://api.hal.science/search/"
OPENAIRE_API = "https://api.openaire.eu/graph/v4/research-products"
BRAVE_API = "https://api.search.brave.com/res/v1/web/search"
OA_RIGHTS = frozenset({"open", "open access", "open source"})


def _urls(
    reference_id: str, urls: Sequence[str], provider: str,
    direct: bool,
) -> List[CandidateSource]:
    """Register only HTTP(S) links, preferring evidenced full-text URLs."""
    items: List[CandidateSource] = []
    seen: set[str] = set()
    for raw in urls:
        if not isinstance(raw, str):
            continue
        url = raw.strip()
        parsed = urlsplit(url)
        if parsed.scheme.lower() not in ("https", "http") or not parsed.hostname or url in seen:
            continue
        host = parsed.hostname.lower()
        if (parsed.username or parsed.password or host == "localhost"
                or host.endswith((".localhost", ".local", ".internal"))):
            continue
        try:
            if not ipaddress.ip_address(host).is_global:
                continue
        except ValueError:
            pass  # An ordinary DNS hostname; no DNS lookup is performed.
        # Metadata-provider redirects to doi.org are typically publisher
        # landing pages, not OA PDFs. Do not treat them as direct sources.
        is_direct = bool(
            direct and parsed.hostname.lower() not in ("doi.org", "dx.doi.org")
        )
        try:
            items.append(CandidateSource(
                reference_id=reference_id, url=url,
                source_type="discovered_oa" if is_direct else "landing_page",
                discovered_via=provider,
                access_confidence=0.72 if is_direct else 0.28,
                identity_confidence=0.78 if is_direct else 0.65,
                expected_cost=1.0 if is_direct else 2.0,
            ))
        except ValueError:
            continue
        seen.add(url)
    return items


def _discover_hal(
    paper: Paper, cfg: AppConfig, registry: SourceRegistry,
    reference_id: str, blocked_providers: Optional[set[str]] = None,
) -> List[CandidateSource]:
    # HAL's Solr query supports exact phrase searches within title_t.
    query = 'title_t:"' + paper.clean_title().replace('"', " ") + '"'
    payload = _get_json(
        registry, reference_id, "HAL", HAL_API,
        {
            "q": query, "wt": "json", "rows": 5,
            "fl": "title_s,authFullName_s,producedDateY_i,fileMain_s,uri_s,openAccess_bool",
        }, cfg, blocked_providers,
    )
    if payload is None:
        return []
    results = (payload.get("response") or {}).get("docs") or []
    candidates: List[CandidateSource] = []
    matches = 0
    for item in results:
        if not isinstance(item, dict):
            continue
        raw_title = item.get("title_s") or ""
        title = raw_title[0] if isinstance(raw_title, list) and raw_title else raw_title
        metadata = {
            "title": title,
            "year": item.get("producedDateY_i"),
            "authors": [{"name": a} for a in (item.get("authFullName_s") or [])],
        }
        if not _matching_paper(paper, metadata, "repository"):
            continue
        matches += 1
        file_url = item.get("fileMain_s")
        if isinstance(file_url, str) and file_url:
            candidates.extend(_urls(reference_id, [file_url], "HAL repository", True))
        landing = item.get("uri_s")
        if isinstance(landing, str) and landing:
            candidates.extend(_urls(reference_id, [landing], "HAL repository", False))
    registry.record_discovery_event(
        reference_id, "HAL", "matched" if matches else "no_match",
        f"matches={matches}, candidates={len(candidates)}",
    )
    return candidates


def _discover_openaire(
    paper: Paper, cfg: AppConfig, registry: SourceRegistry,
    reference_id: str, blocked_providers: Optional[set[str]] = None,
) -> List[CandidateSource]:
    params = {
        "search": paper.clean_title(), "filter": "type:publication",
        "page_size": 5,
    }
    if cfg.unpaywall_email and "@" in cfg.unpaywall_email:
        params["mailto"] = cfg.unpaywall_email
    payload = _get_json(
        registry, reference_id, "OpenAIRE", OPENAIRE_API,
        params, cfg, blocked_providers,
    )
    if payload is None:
        return []
    matches = 0
    candidates: List[CandidateSource] = []
    for item in payload.get("results") or []:
        if not isinstance(item, dict):
            continue
        date = str(item.get("publicationDate") or "")
        authors = item.get("authors") or []
        metadata = {
            "title": item.get("mainTitle"),
            "year": date[:4] if re.match(r"^\d{4}", date) else None,
            "authors": [
                {"name": entry.get("fullName") or ""}
                for entry in authors if isinstance(entry, dict)
            ],
        }
        if not _matching_paper(paper, metadata, "repository"):
            continue
        matches += 1
        for instance in item.get("instances") or []:
            if not isinstance(instance, dict):
                continue
            right = _clean((instance.get("accessRight") or {}).get("label") or "")
            if right not in OA_RIGHTS:
                continue
            for link in instance.get("urls") or []:
                if not isinstance(link, str):
                    continue
                url_path = urlsplit(link).path.lower()
                direct = url_path.endswith(".pdf") or "/pdf/" in url_path
                candidates.extend(_urls(
                    reference_id, [link], "OpenAIRE OA repository", direct,
                ))
    registry.record_discovery_event(
        reference_id, "OpenAIRE", "matched" if matches else "no_match",
        f"matches={matches}, candidates={len(candidates)}",
    )
    return candidates


def _search_result_matches(paper: Paper, result: dict) -> bool:
    """Conservative search-snippet relevance, never proof of PDF identity."""
    title = _clean(paper.clean_title())
    if not title:
        return False
    snippet = _clean(
        str(result.get("title") or "") + " " +
        str(result.get("description") or "")
    )
    # Exact title in search metadata, or most distinctive title tokens in
    # order. Avoid registering unrelated documents from generic keywords.
    if title in snippet:
        return True
    tokens = title.split()
    matched = [token for token in tokens if len(token) > 2 and token in snippet.split()]
    distinctive = [token for token in tokens if len(token) > 2]
    return bool(len(distinctive) >= 5 and len(matched) / len(distinctive) >= 0.85)


def _discover_author_web(
    paper: Paper, cfg: AppConfig, registry: SourceRegistry,
    reference_id: str, blocked_providers: Optional[set[str]] = None,
) -> List[CandidateSource]:
    # Optional: querying a commercial search API may consume paid quota.
    # Never call it without the user's separately configured key.
    if not os.environ.get("BRAVE_SEARCH_API_KEY"):
        return []
    title = paper.clean_title().replace('"', " ").strip()
    if not title:
        return []
    payload = _get_json(
        registry, reference_id, "Brave", BRAVE_API,
        {"q": f'"{title}" filetype:pdf', "count": 10},
        cfg, blocked_providers,
    )
    if payload is None:
        return []
    candidates: List[CandidateSource] = []
    matched = 0
    for item in (payload.get("web") or {}).get("results") or []:
        if not isinstance(item, dict) or not _search_result_matches(paper, item):
            continue
        matched += 1
        url = item.get("url")
        if not isinstance(url, str):
            continue
        path = urlsplit(url).path.lower()
        is_pdf = path.endswith(".pdf") or "/pdf/" in path or "[pdf]" in str(item.get("title") or "").lower()
        # Filetype search alone is not evidence of an actual PDF response:
        # all results must pass the existing downloader's PDF-byte check.
        candidates.extend(_urls(reference_id, [url], "Brave web PDF search", is_pdf))
    registry.record_discovery_event(
        reference_id, "Brave", "matched" if matched else "no_match",
        f"matching_results={matched}, candidates={len(candidates)}",
    )
    return candidates


def discover_repository_sources(
    paper: Paper, cfg: AppConfig, registry: SourceRegistry,
    reference_id: str, blocked_providers: Optional[set[str]] = None,
) -> int:
    """Gather evidenced OA repository and author-hosted candidates."""
    candidates = _discover_hal(paper, cfg, registry, reference_id, blocked_providers)
    candidates.extend(_discover_openaire(paper, cfg, registry, reference_id, blocked_providers))
    candidates.extend(_discover_author_web(paper, cfg, registry, reference_id, blocked_providers))
    known = {candidate.url for candidate in registry.list_sources(reference_id)}
    added = 0
    for candidate in candidates:
        if candidate.url in known:
            continue
        registry.upsert(candidate)
        known.add(candidate.url)
        added += 1
    return added
