"""Conservative, evidence-retaining PDF bibliographic identity screening.

Only text extraction and embedded metadata are used: no network/LLM/OCR.
'verified' means the strict heuristic passed; it is not cryptographic proof.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Dict

from pypdf import PdfReader

from .models import Paper


def _normalize(value: str) -> str:
    ascii_text = unicodedata.normalize("NFKD", value or "").encode("ascii", "ignore").decode("ascii")
    return " ".join(re.findall(r"[a-z0-9]+", ascii_text.lower()))


def _first_author(paper: Paper) -> str:
    authors = (paper.authors or "").strip()
    if not authors or authors.lower() == "unknown":
        return ""
    first = re.split(r"\s+and\s+|\s*;\s*|\s*,\s*", authors, maxsplit=1, flags=re.IGNORECASE)[0]
    return _normalize(first).split()[-1] if _normalize(first) else ""


def _canonical_doi(value: str) -> str:
    text = (value or "").strip().lower()
    return re.sub(r"^https?://(?:dx\.)?doi\.org/", "", text).rstrip(".,;)]}")


@dataclass(frozen=True)
class IdentityAssessment:
    status: str
    evidence: Dict[str, object]


def assess_pdf_identity(paper: Paper, path: str | Path) -> IdentityAssessment:
    """Check paper title, first-author surname, year and DOI.

    A conflict requires explicit contradictory metadata or header DOI. Missing,
    inaccessible or insufficient evidence yields needs_review, not mismatch.
    """
    title = _normalize(paper.clean_title())
    author = _first_author(paper)
    expected_doi = _canonical_doi(paper.doi)
    try:
        reader = PdfReader(str(path))
        if not reader.pages:
            raise ValueError("PDF has no pages")
        first = reader.pages[0].extract_text() or ""
        info = reader.metadata or {}
        embedded_title = str(info.get("/Title") or "")
        embedded_author = str(info.get("/Author") or "")
        text = _normalize(first[:12000])
        header = first[:4000]
    except Exception as exc:
        return IdentityAssessment("needs_review", {
            "reason": "pdf_text_unavailable",
            "detail": type(exc).__name__,
            "expected_title": paper.clean_title(),
        })

    header_doi = re.search(
        r"\b(?:doi|digital\s+object\s+identifier)\s*:?\s*"
        r"(10\.\d{4,9}/[A-Za-z0-9._;()/:+-]+)",
        header, flags=re.IGNORECASE,
    )
    found_doi = _canonical_doi(header_doi.group(1)) if header_doi else ""
    title_found = bool(title and title in text)
    author_text = _normalize(first[:4000] + " " + embedded_author)
    author_found = bool(author and author in author_text.split())
    year_found = bool(paper.year and str(paper.year) in first[:5000])
    doi_found = bool(expected_doi and expected_doi in _canonical_doi(header))
    # Restrict contradictory-title evidence to a meaningful embedded title
    # also explicitly visible in the document's first-page heading area.
    actual_title = _normalize(embedded_title)
    metadata_conflict = bool(
        len(actual_title) >= 20
        and actual_title in _normalize(first[:2500])
        and not title_found
        and title != actual_title
    )
    doi_conflict = bool(expected_doi and found_doi and found_doi != expected_doi)
    evidence: Dict[str, object] = {
        "expected_title": paper.clean_title(),
        "expected_doi": paper.doi,
        "expected_year": paper.year,
        "expected_first_author": author,
        "pdf_metadata_title": embedded_title[:240],
        "pdf_metadata_author": embedded_author[:240],
        "header_doi": found_doi,
        "title_found": title_found,
        "author_found": author_found,
        "year_found": year_found,
        "doi_found": doi_found,
        "metadata_title_conflict": metadata_conflict,
        "doi_conflict": doi_conflict,
    }
    if doi_conflict or metadata_conflict:
        status, reason = "mismatch", "contradictory_pdf_identity"
    elif title_found and author_found and (year_found or doi_found or not paper.year):
        status, reason = "verified", "title_author_and_year_or_doi"
    else:
        status, reason = "needs_review", "insufficient_positive_evidence"
    evidence["reason"] = reason
    return IdentityAssessment(status, evidence)
