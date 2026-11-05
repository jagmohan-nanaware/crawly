"""Multi-source PDF downloader & DOI resolver for Crawly (requests + BeautifulSoup4, configured via config.json)."""

from __future__ import annotations

import csv
import difflib
import hashlib
import json
import re
import urllib.parse
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

import requests
from bs4 import BeautifulSoup

from .models import DEFAULT_API_ENDPOINTS, AppConfig, Paper, save_papers_to_csv
from .identity import assess_pdf_identity
from .source_registry import CandidateSource, SourceRegistry, is_excluded_reference
from .sources import get_browser_headers, get_polite_headers, normalize_doi, reconstruct_openalex_abstract


def _title_similarity(a: str, b: str) -> float:
    def _clean(s: str) -> str:
        s = re.sub(r"\[(BOOK|HTML|B|PDF|CITATION|DOC)\]", "", str(s), flags=re.IGNORECASE)
        s = re.sub(r"[^a-z0-9 ]+", " ", s.lower())
        return re.sub(r"\s+", " ", s).strip()

    ca, cb = _clean(a), _clean(b)
    if not ca or not cb:
        return 0.0
    if ca == cb or (len(ca) > 20 and (ca in cb or cb in ca)):
        return 1.0
    return difflib.SequenceMatcher(None, ca, cb).ratio()


def extract_doi_from_text_or_url(text: str) -> str:
    if not text or not isinstance(text, str):
        return ""
    decoded = urllib.parse.unquote(text)
    m = re.search(r"(10\.\d{4,9}/[-._;()/:A-Za-z0-9]+)", decoded)
    if not m:
        return ""
    doi = m.group(1).rstrip(".,;)/")
    for suffix in (".pdf", ".short", ".full", "/meta", "/full", "/pdf", "/abstract"):
        if doi.lower().endswith(suffix):
            doi = doi[: -len(suffix)]
    return normalize_doi(doi)


def resolve_paper_metadata(paper: Paper, timeout: int = 10, config: Optional[AppConfig] = None) -> Paper:
    cfg = config or AppConfig.load()
    if not paper.doi:
        paper.doi = extract_doi_from_text_or_url(paper.url) or extract_doi_from_text_or_url(paper.title)

    clean_t = paper.clean_title()
    oa_base = cfg.api_endpoints.get("openalex_works", DEFAULT_API_ENDPOINTS["openalex_works"]).rstrip("/")
    cr_base = cfg.api_endpoints.get("crossref_works", DEFAULT_API_ENDPOINTS["crossref_works"]).rstrip("/")
    polite_hdrs = get_polite_headers(cfg)

    try:
        if paper.doi:
            oa_api = f"{oa_base}/https://doi.org/{urllib.parse.quote(paper.doi, safe='/')}"
            r = requests.get(oa_api, headers=polite_hdrs, timeout=timeout)
            if r.status_code == 200:
                item = r.json()
                best_oa = item.get("best_oa_location") or {}
                prim = item.get("primary_location") or {}
                oa_info = item.get("open_access") or {}
                candidate_pdf = best_oa.get("pdf_url") or prim.get("pdf_url") or oa_info.get("oa_url") or ""
                if candidate_pdf and not paper.pdf_url:
                    paper.pdf_url = candidate_pdf
                if not paper.abstract:
                    paper.abstract = reconstruct_openalex_abstract(item.get("abstract_inverted_index"))
                if not paper.citations and item.get("cited_by_count"):
                    paper.citations = int(item["cited_by_count"])
        if not paper.doi and clean_t:
            r = requests.get(
                oa_base,
                params={"search": clean_t, "per-page": 3},
                headers=polite_hdrs,
                timeout=timeout,
            )
            if r.status_code == 200:
                for item in r.json().get("results") or []:
                    cand_title = item.get("title") or item.get("display_name") or ""
                    if _title_similarity(clean_t, cand_title) >= 0.70:
                        found_doi = normalize_doi(item.get("doi"))
                        if found_doi:
                            paper.doi = found_doi
                        best_oa = item.get("best_oa_location") or {}
                        prim = item.get("primary_location") or {}
                        oa_info = item.get("open_access") or {}
                        candidate_pdf = best_oa.get("pdf_url") or prim.get("pdf_url") or oa_info.get("oa_url") or ""
                        if candidate_pdf and not paper.pdf_url:
                            paper.pdf_url = candidate_pdf
                        if not paper.abstract:
                            paper.abstract = reconstruct_openalex_abstract(item.get("abstract_inverted_index"))
                        if not paper.citations and item.get("cited_by_count"):
                            paper.citations = int(item["cited_by_count"])
                        break
    except Exception:
        pass

    if not paper.doi and clean_t:
        try:
            r = requests.get(
                cr_base,
                params={"query.bibliographic": clean_t, "rows": 3},
                headers=polite_hdrs,
                timeout=timeout,
            )
            if r.status_code == 200:
                items = (r.json().get("message") or {}).get("items") or []
                for item in items:
                    titles = item.get("title") or []
                    cand_title = titles[0] if titles else ""
                    if _title_similarity(clean_t, cand_title) >= 0.72:
                        paper.doi = normalize_doi(item.get("DOI"))
                        for link in item.get("link") or []:
                            if "pdf" in (link.get("content-type") or "").lower() and not paper.pdf_url:
                                paper.pdf_url = link.get("URL") or ""
                        break
        except Exception:
            pass

    return paper


def _extract_pdf_url_from_html(html: str, base_url: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for selector in [
        ("embed", {"id": "pdf"}),
        ("iframe", {"id": "pdf"}),
        ("embed", {"type": "application/pdf"}),
        ("iframe", {}),
        ("embed", {}),
    ]:
        tag = soup.find(selector[0], selector[1])
        if tag and tag.get("src"):
            src = tag["src"].strip().split("#")[0].strip()
            if src:
                return urllib.parse.urljoin(base_url, src)

    for btn in soup.find_all(["button", "a"]):
        onclick = btn.get("onclick") or ""
        m = re.search(r"location\.href\s*=\s*['\"]([^'\"]+)['\"]", onclick)
        if m:
            raw = m.group(1).replace("\\/", "/").split("#")[0].strip()
            if raw:
                return urllib.parse.urljoin(base_url, raw)

    meta_pdf = soup.find("meta", attrs={"name": re.compile(r"citation_pdf_url", re.I)})
    if meta_pdf and meta_pdf.get("content"):
        return urllib.parse.urljoin(base_url, meta_pdf["content"].strip())

    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if ".pdf" in href.lower() and not href.lower().startswith("javascript:"):
            return urllib.parse.urljoin(base_url, href.split("#")[0])

    return ""


def _fetch_and_verify_pdf(
    url: str,
    target_path: Path,
    timeout: int = 20,
    config: Optional[AppConfig] = None,
    status_callback: Optional[Callable[[str], None]] = None,
) -> bool:
    """Retrieve PDF bytes; report transport status separately from paper identity.

    A successful PDF signature check means only 'pdf_received', never
    'verified'. Title/author/DOI identity verification is a separate gate.
    """
    def note(outcome: str) -> None:
        if status_callback is not None:
            status_callback(outcome)

    def http_outcome(code: int) -> str:
        return {
            401: "authentication_required",
            403: "access_denied",
            404: "not_found",
            429: "rate_limited",
        }.get(code, "download_error")

    if not url or not url.startswith(("http://", "https://")):
        note("download_error")
        return False
    cfg = config or AppConfig.load()
    headers = {
        "User-Agent": get_browser_headers(cfg)["User-Agent"],
        "Accept": "application/pdf,application/octet-stream;q=0.9,*/*;q=0.8",
    }
    try:
        resp = requests.get(url, headers=headers, timeout=timeout, allow_redirects=True, stream=True)
        if resp.status_code != 200:
            note(http_outcome(resp.status_code))
            return False

        chunks = []
        total_bytes = 0
        first_chunk = b""
        for chunk in resp.iter_content(chunk_size=16384):
            if not chunk:
                continue
            if not first_chunk:
                first_chunk = chunk
            chunks.append(chunk)
            total_bytes += len(chunk)
            if b"%PDF-" not in first_chunk[:1024] and total_bytes > 262144:
                break

        content = b"".join(chunks)
        pdf_idx = content.find(b"%PDF-")
        if 0 <= pdf_idx <= 1024 and len(content) > 1024:
            target_path.parent.mkdir(parents=True, exist_ok=True)
            target_path.write_bytes(content[pdf_idx:])
            note("pdf_received")
            return True

        ctype = (resp.headers.get("Content-Type") or "").lower()
        if "html" in ctype or content.lstrip().startswith((b"<!DOCTYPE", b"<html", b"<HTML")):
            html_text = content.decode("utf-8", errors="ignore")
            nested_pdf_url = _extract_pdf_url_from_html(html_text, resp.url)
            if nested_pdf_url and nested_pdf_url != url:
                resp2 = requests.get(
                    nested_pdf_url,
                    headers={**headers, "Referer": resp.url},
                    timeout=timeout,
                    allow_redirects=True,
                )
                if resp2.status_code == 200:
                    idx2 = resp2.content.find(b"%PDF-")
                    if 0 <= idx2 <= 1024 and len(resp2.content) > 1024:
                        target_path.parent.mkdir(parents=True, exist_ok=True)
                        target_path.write_bytes(resp2.content[idx2:])
                        note("pdf_received")
                        return True
                else:
                    note(http_outcome(resp2.status_code))
                    return False
    except Exception:
        note("download_error")
        return False
    note("download_error")
    return False


def _candidate_publisher_oa_urls(paper: Paper) -> List[Tuple[str, str]]:
    candidates: List[Tuple[str, str]] = []
    if paper.pdf_url:
        candidates.append(("Direct/OA Link", paper.pdf_url))
    u = (paper.url or "").strip()
    if "arxiv.org/abs/" in u:
        candidates.append(("arXiv Direct", u.replace("/abs/", "/pdf/") + ".pdf"))
    if "mdpi.com/" in u and not u.endswith("/pdf"):
        candidates.append(("MDPI Open Access", u.rstrip("/") + "/pdf"))
    return candidates


def _candidate_unpaywall_urls(
    doi: str,
    email: str,
    timeout: int = 10,
    config: Optional[AppConfig] = None,
) -> List[Tuple[str, str]]:
    if not doi:
        return []
    cfg = config or AppConfig.load()
    out: List[Tuple[str, str]] = []
    base = cfg.api_endpoints.get("unpaywall_base", DEFAULT_API_ENDPOINTS["unpaywall_base"]).rstrip("/")
    url = f"{base}/{urllib.parse.quote(doi, safe='/')}"
    try:
        r = requests.get(url, params={"email": email}, headers=get_polite_headers(cfg), timeout=timeout)
        if r.status_code == 200:
            data = r.json()
            best = data.get("best_oa_location") or {}
            if best.get("url_for_pdf"):
                out.append(("Unpaywall OA", best["url_for_pdf"]))
            for loc in data.get("oa_locations") or []:
                u = loc.get("url_for_pdf")
                if u and all(u != existing[1] for existing in out):
                    out.append(("Unpaywall OA", u))
    except Exception:
        pass
    return out


def _candidate_semantic_scholar_urls(
    paper: Paper,
    api_key: str = "",
    timeout: int = 10,
    config: Optional[AppConfig] = None,
) -> List[Tuple[str, str]]:
    cfg = config or AppConfig.load()
    out: List[Tuple[str, str]] = []
    headers = get_polite_headers(cfg)
    if api_key:
        headers["x-api-key"] = api_key
    s2_base = cfg.api_endpoints.get("semantic_scholar_paper", DEFAULT_API_ENDPOINTS["semantic_scholar_paper"]).rstrip("/")
    try:
        if paper.doi:
            r = requests.get(
                f"{s2_base}/DOI:{urllib.parse.quote(paper.doi, safe='/')}",
                params={"fields": "openAccessPdf,externalIds"},
                headers=headers,
                timeout=timeout,
            )
            if r.status_code == 200:
                item = r.json()
                oa = (item.get("openAccessPdf") or {}).get("url")
                if oa:
                    out.append(("Semantic Scholar OA", oa))
                arxiv_id = (item.get("externalIds") or {}).get("ArXiv")
                if arxiv_id:
                    out.append(("arXiv Preprint", f"https://arxiv.org/pdf/{arxiv_id}.pdf"))
    except Exception:
        pass
    return out


def _candidate_europe_pmc_urls(
    paper: Paper,
    timeout: int = 10,
    config: Optional[AppConfig] = None,
) -> List[Tuple[str, str]]:
    cfg = config or AppConfig.load()
    out: List[Tuple[str, str]] = []
    q = f'DOI:"{paper.doi}"' if paper.doi else f'TITLE:"{paper.clean_title()}"'
    epmc_search = cfg.api_endpoints.get("europe_pmc_search", DEFAULT_API_ENDPOINTS["europe_pmc_search"])
    epmc_pdf = cfg.api_endpoints.get("europe_pmc_pdf", DEFAULT_API_ENDPOINTS["europe_pmc_pdf"])
    try:
        r = requests.get(
            epmc_search,
            params={"query": q, "format": "json", "pageSize": 1},
            headers=get_polite_headers(cfg),
            timeout=timeout,
        )
        if r.status_code == 200:
            results = ((r.json().get("resultList") or {}).get("result")) or []
            if results:
                pmcid = results[0].get("pmcid")
                if pmcid:
                    out.append(
                        (
                            "Europe PMC",
                            f"{epmc_pdf}?accid={pmcid}&blobtype=pdf",
                        )
                    )
    except Exception:
        pass
    return out


def _try_scihub_download(
    paper: Paper,
    target_path: Path,
    mirrors: Sequence[str],
    timeout: int = 15,
    config: Optional[AppConfig] = None,
) -> Optional[str]:
    cfg = config or AppConfig.load()
    doi = normalize_doi(paper.doi)
    if doi and cfg.scihub_cdn_url:
        cdn_url = cfg.scihub_cdn_url.replace("{doi}", doi)
        if _fetch_and_verify_pdf(cdn_url, target_path, timeout=timeout, config=cfg):
            cdn_host = urllib.parse.urlparse(cdn_url).netloc or "CDN"
            return f"Sci-Hub CDN ({cdn_host})"

    queries = []
    if doi:
        queries.append(doi)
    if paper.url and paper.url != "NA":
        queries.append(paper.url)

    for mirror in mirrors:
        m_base = mirror.strip().rstrip("/")
        if not m_base:
            continue
        if not m_base.startswith(("http://", "https://")):
            m_base = "https://" + m_base
        for q in queries:
            lookup_url = f"{m_base}/{q}"
            if _fetch_and_verify_pdf(lookup_url, target_path, timeout=timeout, config=cfg):
                return f"Sci-Hub ({urllib.parse.urlparse(m_base).netloc})"

    return None




def _identity_check_and_archive(
    paper: Paper,
    target_path: Path,
    url: str,
    registry: Optional[SourceRegistry],
    reference_id: str,
) -> str:
    """Keep unverified PDFs out of the accepted directory while retaining evidence."""
    assessment = assess_pdf_identity(paper, target_path)
    evidence = dict(assessment.evidence)
    if assessment.status != "verified":
        if evidence.get("reason") == "pdf_text_unavailable":
            review_reason = "PDF text could not be extracted; inspect the candidate manually"
        elif assessment.status == "mismatch":
            review_reason = "Conflicting PDF title or DOI; inspect the candidate manually"
        else:
            missing = []
            if not evidence.get("title_found"):
                missing.append("title not matched")
            if not evidence.get("author_found"):
                missing.append("first author not matched")
            if not (evidence.get("year_found") or evidence.get("doi_found")):
                missing.append("year and DOI not confirmed")
            review_reason = "; ".join(missing) or "Bibliographic evidence insufficient"
        evidence["review_reason"] = review_reason
        archive_dir = target_path.parent / "identity_review" / assessment.status
        archive_dir.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:10]
        basename = f"{target_path.stem}--{digest}"
        archived = archive_dir / f"{basename}.pdf"
        index = 1
        while archived.exists():
            archived = archive_dir / f"{basename}-{index}.pdf"
            index += 1
        target_path.replace(archived)
        evidence["review_pdf_path"] = str(archived)
    paper.extra_fields["PDF Identity Status"] = assessment.status
    paper.extra_fields["PDF Identity Evidence"] = json.dumps(evidence, sort_keys=True)
    paper.extra_fields["PDF Review Reason"] = evidence.get("review_reason", "")
    if registry is not None and url != "local-cache":
        registry.record_identity_check(reference_id, url, assessment.status, evidence)
    return assessment.status


def _download_from_registry(
    paper: Paper,
    target_path: Path,
    cfg: AppConfig,
    registry: SourceRegistry,
    blocked_hosts: set[str],
    custom_scihub_url: Optional[str],
) -> Tuple[bool, str, Optional[Path]]:
    """Try recorded sources first, then discover OA fallback sources.

    PDF byte validation is distinct from bibliographic identity checks.
    The strict gate runs only when cfg.verify_pdf_identity is enabled.
    """
    ref_id = (paper.extra_fields.get("MCS Ref No") or paper.doi or paper.clean_title()).strip()
    registry.import_paper(paper)
    tried: set[str] = set()
    # A reviewable PDF is still an acquired candidate. Keep the first one
    # available while trying to find a bibliographically verified alternative.
    review_candidate: Optional[Tuple[str, str, str, str]] = None

    def remember_review(source_label: str) -> None:
        nonlocal review_candidate
        if review_candidate is None:
            evidence_json = paper.extra_fields["PDF Identity Evidence"]
            evidence = json.loads(evidence_json)
            review_candidate = (
                evidence["review_pdf_path"],
                source_label,
                evidence_json,
                paper.extra_fields["PDF Review Reason"],
            )

    def try_sources(include_landing: bool = False) -> Tuple[bool, str, Optional[Path]]:
        for candidate in registry.list_sources(ref_id):
            # A publisher landing page can expose an access barrier. Prefer
            # official OA metadata discovery before requesting landing pages.
            if candidate.source_type == "landing_page" and not include_landing:
                continue
            if candidate.url in tried:
                continue
            host = urllib.parse.urlparse(candidate.url).hostname or ""
            if host.lower() in blocked_hosts:
                continue
            tried.add(candidate.url)
            outcomes: List[str] = []
            ok = _fetch_and_verify_pdf(
                candidate.url,
                target_path,
                timeout=cfg.request_timeout + 8,
                config=cfg,
                status_callback=outcomes.append,
            )
            outcome = outcomes[-1] if outcomes else ("pdf_received" if ok else "download_error")
            accepted = ok
            if ok and cfg.verify_pdf_identity:
                decision = _identity_check_and_archive(
                    paper, target_path, candidate.url, registry, ref_id,
                )
                outcome = "verified" if decision == "verified" else (
                    "identity_mismatch" if decision == "mismatch" else "pdf_received"
                )
                accepted = decision == "verified"
                if decision == "needs_review":
                    label = candidate.discovered_via if candidate.discovered_via != "csv" else candidate.source_type
                    remember_review(f"Registry: {label}")
            registry.record_attempt(ref_id, candidate.url, outcome)
            if outcome in ("rate_limited", "access_denied", "authentication_required"):
                blocked_hosts.add(host.lower())
            if accepted:
                paper.local_pdf = str(target_path)
                label = candidate.discovered_via if candidate.discovered_via != "csv" else candidate.source_type
                paper.download_source = f"Registry: {label}"
                return True, paper.download_source, target_path
        return False, "Not Found", None

    first = try_sources()
    if first[0]:
        return first

    resolve_paper_metadata(paper, timeout=cfg.request_timeout, config=cfg)
    discovered = _candidate_publisher_oa_urls(paper)
    if paper.doi:
        discovered.extend(_candidate_unpaywall_urls(
            paper.doi, cfg.unpaywall_email, timeout=cfg.request_timeout, config=cfg
        ))
        discovered.extend(_candidate_semantic_scholar_urls(
            paper, api_key=cfg.semantic_scholar_api_key, timeout=cfg.request_timeout, config=cfg
        ))
    discovered.extend(_candidate_europe_pmc_urls(
        paper, timeout=cfg.request_timeout, config=cfg
    ))

    known_urls = {source.url for source in registry.list_sources(ref_id)}
    for label, url in discovered:
        if not url or url in known_urls:
            continue
        registry.upsert(CandidateSource(
            reference_id=ref_id, url=url,
            source_type="discovered_oa", discovered_via=label,
        ))
        known_urls.add(url)

    second = try_sources(include_landing=True)
    if second[0]:
        return second

    if cfg.use_scihub_fallback or custom_scihub_url:
        mirrors = list(cfg.scihub_mirrors)
        if custom_scihub_url:
            clean_m = custom_scihub_url.strip().rstrip("/")
            if clean_m and clean_m not in mirrors:
                mirrors.insert(0, clean_m)
        source = _try_scihub_download(
            paper, target_path, mirrors=mirrors,
            timeout=cfg.request_timeout + 8, config=cfg,
        )
        if source:
            if cfg.verify_pdf_identity:
                decision = _identity_check_and_archive(
                    paper, target_path, source, None, ref_id,
                )
                if decision == "verified":
                    paper.local_pdf = str(target_path)
                    paper.download_source = source
                    return True, source, target_path
                if decision == "needs_review":
                    remember_review(source)
            else:
                paper.local_pdf = str(target_path)
                paper.download_source = source
                return True, source, target_path

    if review_candidate is not None:
        review_path, review_source, evidence_json, review_reason = review_candidate
        paper.local_pdf = review_path
        paper.download_source = review_source
        paper.extra_fields["PDF Identity Status"] = "needs_review"
        paper.extra_fields["PDF Identity Evidence"] = evidence_json
        paper.extra_fields["PDF Review Reason"] = review_reason
        return True, f"Needs Review: {review_source}", Path(review_path)

    if cfg.verify_pdf_identity and paper.extra_fields.get("PDF Identity Status") == "mismatch":
        return False, "Identity Mismatch", None
    return False, "Not Found", None


def download_paper_pdf(
    paper: Paper,
    output_dir: Path,
    config: Optional[AppConfig] = None,
    custom_scihub_url: Optional[str] = None,
    registry: Optional[SourceRegistry] = None,
    blocked_hosts: Optional[set[str]] = None,
) -> Tuple[bool, str, Optional[Path]]:
    cfg = config or AppConfig.load()
    output_dir.mkdir(parents=True, exist_ok=True)
    if cfg.verify_pdf_identity and registry is None:
        registry = SourceRegistry(cfg.source_registry_path or (output_dir / "sources.sqlite"))
    if registry is not None and is_excluded_reference(paper):
        return False, "Excluded document type", None
    if cfg.verify_pdf_identity:
        # An earlier run's CSV success must not survive a failed revalidation.
        paper.extra_fields.pop("PDF Identity Status", None)
        paper.extra_fields.pop("PDF Identity Evidence", None)
        paper.local_pdf = ""
        paper.download_source = ""
    target_path = output_dir / paper.safe_filename()

    if target_path.exists() and target_path.stat().st_size > 1024:
        try:
            with target_path.open("rb") as fh:
                head = fh.read(1024)
            if b"%PDF-" in head:
                if cfg.verify_pdf_identity:
                    reference_id = (paper.extra_fields.get("MCS Ref No") or paper.doi or paper.clean_title()).strip()
                    decision = _identity_check_and_archive(
                        paper, target_path, "local-cache", None, reference_id,
                    )
                    if decision == "verified":
                        paper.local_pdf = str(target_path)
                        paper.download_source = paper.download_source or "Local Cache"
                        return True, paper.download_source, target_path
                    paper.local_pdf = ""
                else:
                    paper.local_pdf = str(target_path)
                    paper.download_source = paper.download_source or "Local Cache"
                    return True, paper.download_source, target_path
        except Exception:
            pass

    if registry is not None:
        return _download_from_registry(
            paper, target_path, cfg, registry,
            blocked_hosts if blocked_hosts is not None else set(),
            custom_scihub_url,
        )

    resolve_paper_metadata(paper, timeout=cfg.request_timeout, config=cfg)

    candidates: List[Tuple[str, str]] = []
    candidates.extend(_candidate_publisher_oa_urls(paper))
    if paper.doi:
        candidates.extend(_candidate_unpaywall_urls(paper.doi, cfg.unpaywall_email, timeout=cfg.request_timeout, config=cfg))
        candidates.extend(
            _candidate_semantic_scholar_urls(
                paper, api_key=cfg.semantic_scholar_api_key, timeout=cfg.request_timeout, config=cfg
            )
        )
    candidates.extend(_candidate_europe_pmc_urls(paper, timeout=cfg.request_timeout, config=cfg))

    tried_urls = set()
    for source_label, cand_url in candidates:
        if not cand_url or cand_url in tried_urls:
            continue
        tried_urls.add(cand_url)
        if _fetch_and_verify_pdf(cand_url, target_path, timeout=cfg.request_timeout + 8, config=cfg):
            paper.local_pdf = str(target_path)
            paper.download_source = source_label
            return True, source_label, target_path

    if cfg.use_scihub_fallback or custom_scihub_url:
        mirrors = list(cfg.scihub_mirrors)
        if custom_scihub_url:
            clean_m = custom_scihub_url.strip().rstrip("/")
            if clean_m and clean_m not in mirrors:
                mirrors.insert(0, clean_m)
        sh_source = _try_scihub_download(
            paper,
            target_path,
            mirrors=mirrors,
            timeout=cfg.request_timeout + 8,
            config=cfg,
        )
        if sh_source:
            paper.local_pdf = str(target_path)
            paper.download_source = sh_source
            return True, sh_source, target_path

    return False, "Not Found", None


def download_batch_pdfs(
    papers: Sequence[Paper],
    output_dir: str | Path,
    csv_path: Optional[str | Path] = None,
    config: Optional[AppConfig] = None,
    custom_scihub_url: Optional[str] = None,
    progress_callback: Optional[Callable[[int, int, Paper, bool, str], None]] = None,
) -> Dict[str, int]:
    cfg = config or AppConfig.load()
    out_path = Path(str(output_dir).strip())
    out_path.mkdir(parents=True, exist_ok=True)

    downloaded = 0
    not_found_rows: List[Dict[str, str]] = []
    skipped = 0
    by_source: Dict[str, int] = {}

    registry = SourceRegistry(cfg.source_registry_path or (out_path / "sources.sqlite")) if (cfg.use_source_registry or cfg.verify_pdf_identity) else None
    blocked_hosts: set[str] = set()
    total = len(papers)
    for idx, paper in enumerate(papers, start=1):
        kwargs = {"registry": registry, "blocked_hosts": blocked_hosts} if registry is not None else {}
        ok, src, _ = download_paper_pdf(
            paper,
            output_dir=out_path,
            config=cfg,
            custom_scihub_url=custom_scihub_url,
            **kwargs,
        )
        if ok:
            downloaded += 1
            by_source[src] = by_source.get(src, 0) + 1
        elif src == "Excluded document type" and registry is not None:
            skipped += 1
        else:
            not_found_rows.append(
                {
                    "Paper Title": paper.clean_title(),
                    "Paper URL": paper.url,
                    "DOI": paper.doi,
                }
            )
        if progress_callback:
            progress_callback(idx, total, paper, ok, src)

    nf_path = out_path / "papers_not_found.csv"
    with nf_path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=["Paper Title", "Paper URL", "DOI"])
        writer.writeheader()
        writer.writerows(not_found_rows)

    if csv_path:
        try:
            save_papers_to_csv(list(papers), csv_path)
        except Exception:
            pass

    stats = {
        "total": total,
        "downloaded": downloaded,
        "missing": len(not_found_rows),
        **by_source,
    }
    if registry is not None:
        stats["skipped"] = skipped
    if cfg.verify_pdf_identity:
        stats["verified"] = sum(p.extra_fields.get("PDF Identity Status") == "verified" for p in papers)
        stats["needs_review"] = sum(p.extra_fields.get("PDF Identity Status") == "needs_review" for p in papers)
        stats["identity_mismatch"] = sum(p.extra_fields.get("PDF Identity Status") == "mismatch" for p in papers)
    return stats


def sync_dataset_to_zotero(
    papers: Sequence[Paper],
    collection_name: str,
    output_dir: Optional[Path] = None,
    config: Optional[AppConfig] = None,
) -> Tuple[bool, str, Path]:
    """Sync dataset papers to local Zotero desktop or produce a Zotero-ready bundle with linked PDFs.

    Returns:
        (is_live_connected, status_message, zotero_bib_path)
    """
    cfg = config or AppConfig.load()
    out_dir = output_dir or Path.cwd()
    out_dir.mkdir(parents=True, exist_ok=True)

    slug = re.sub(r"[^a-zA-Z0-9_\-]+", "_", collection_name).strip("_") or "dataset"
    zotero_bib = out_dir / f"Zotero_Import_{slug}.bib"

    # 1. Build Zotero-compatible BibTeX entries with linked PDF attachments (file = {path:application/pdf})
    entries: List[str] = []
    for idx, p in enumerate(papers, start=1):
        ckey = p.bibtex_key() or f"paper_{idx}"
        lines = [f"@article{{{ckey},"]
        lines.append(f"  title = {{{p.clean_title()}}},")
        if p.authors:
            lines.append(f"  author = {{{p.authors}}},")
        if p.year > 0:
            lines.append(f"  year = {{{p.year}}},")
        if p.publication:
            lines.append(f"  journal = {{{p.publication}}},")
        if p.doi:
            lines.append(f"  doi = {{{p.doi}}},")
        if p.url:
            lines.append(f"  url = {{{p.url}}},")

        pdf_p = Path(p.local_pdf) if p.local_pdf else (out_dir / p.safe_filename())
        if pdf_p.exists():
            abs_pdf = pdf_p.resolve()
            lines.append(f"  file = {{{abs_pdf}:application/pdf}},")
        lines.append("}")
        entries.append("\n".join(lines))

    zotero_bib.write_text("\n\n".join(entries), encoding="utf-8")

    # 2. Probe local Zotero desktop connector HTTP interface
    is_live = False
    status_msg = ""
    try:
        r = requests.get("http://127.0.0.1:23119/connector/ping", timeout=2.0)
        if r.status_code == 200:
            is_live = True
            status_msg = (
                f"Zotero desktop is ONLINE at localhost:23119. Generated Zotero-ready bundle '{zotero_bib.name}' "
                f"with {len(papers)} items and full-text PDF attachments."
            )
        else:
            status_msg = f"Zotero responded with HTTP {r.status_code}."
    except Exception:
        is_live = False
        status_msg = (
            f"Zotero desktop is not running locally (port 23119). Generated Zotero bundle '{zotero_bib.name}'. "
            f"Open Zotero and select File -> Import -> select '{zotero_bib.name}' to automatically import all citations and linked PDFs."
        )

    return is_live, status_msg, zotero_bib

