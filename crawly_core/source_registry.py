"""Persistent multi-source candidates and evidence-aware acquisition ranking.

The scores here are *heuristics*, not calibrated probabilities. Nothing in this
module downloads a file or attempts to bypass a publisher's access policy.
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import List
from urllib.parse import urlsplit

from .models import Paper


OUTCOMES = frozenset({
    "verified", "pdf_received", "identity_mismatch", "not_found",
    "authentication_required", "rate_limited", "access_denied", "download_error",
})
PDF_OUTCOMES = frozenset({"verified", "pdf_received", "identity_mismatch"})


@dataclass(frozen=True)
class CandidateSource:
    """One potential URL for one reference, plus observations about attempts."""

    reference_id: str
    url: str
    source_type: str = "unknown"
    discovered_via: str = "csv"
    access_confidence: float = 0.5
    identity_confidence: float = 0.5
    expected_cost: float = 1.0
    attempts: int = 0
    pdf_responses: int = 0
    verified_downloads: int = 0
    last_outcome: str = ""
    last_attempt_utc: str = ""

    def __post_init__(self) -> None:
        parsed = urlsplit(self.url.strip())
        if not self.reference_id.strip() or parsed.scheme.lower() not in ("http", "https") or not parsed.hostname:
            raise ValueError("A candidate source needs a reference ID and an HTTP(S) URL")
        for name in ("access_confidence", "identity_confidence"):
            value = getattr(self, name)
            if not math.isfinite(value) or not 0.0 <= value <= 1.0:
                raise ValueError(f"{name} must be between 0 and 1")
        if not math.isfinite(self.expected_cost) or self.expected_cost <= 0:
            raise ValueError("expected_cost must be positive")
        if not (0 <= self.verified_downloads <= self.pdf_responses <= self.attempts):
            raise ValueError("inconsistent attempt counters")

    @property
    def priority_score(self) -> float:
        """Cost-normalized heuristic using a two-observation prior.

        Failed or successful attempts inform ranking but do not certify access,
        since results can depend on authentication, environment and time.
        """
        access = (2 * self.access_confidence + self.pdf_responses) / (2 + self.attempts)
        identity = (2 * self.identity_confidence + self.verified_downloads) / (2 + self.pdf_responses)
        return access * identity / self.expected_cost


def is_excluded_reference(paper: Paper) -> bool:
    """Only explicitly classified non-literature references are excluded."""
    kind = (paper.extra_fields.get("Document Type") or paper.extra_fields.get("Reference Type") or "").strip().lower()
    return kind in {"software", "software repository", "source code", "datasheet", "data sheet", "dataset"}


def candidates_from_paper(paper: Paper) -> List[CandidateSource]:
    """Extract the primary and numbered alternate URLs from Crawly's CSV model.

    Optional CSV columns: 'Access Confidence N', 'Identity Confidence N',
    and 'Expected Cost N'. N=1 is the primary PDF URL; N>=2 is an alternate.
    Other project-specific CSV columns are preserved by the Paper model.
    """
    extras = paper.extra_fields
    if is_excluded_reference(paper):
        return []
    reference_id = (extras.get("MCS Ref No") or paper.doi or paper.clean_title()).strip()
    urls = [(1, paper.pdf_url, "primary_pdf")]
    for key, value in extras.items():
        match = re.fullmatch(r"(?:Source|PDF) URL (\d+)", key, flags=re.IGNORECASE)
        if match:
            urls.append((int(match.group(1)), value, "alternate"))
    if paper.url and paper.url != "NA":
        urls.append((0, paper.url, "landing_page"))

    candidates: List[CandidateSource] = []
    seen = set()
    for number, url, source_type in urls:
        url = (url or "").strip()
        if not url or url in seen:
            continue
        seen.add(url)
        if source_type == "landing_page":
            defaults = (0.3, 0.6, 2.0)
        else:
            defaults = (0.5, 0.5, 1.0)
        values = (
            extras.get(f"Access Confidence {number}"),
            extras.get(f"Identity Confidence {number}"),
            extras.get(f"Expected Cost {number}"),
        )
        # A malformed alternate URL or CSV score must not abort an entire
        # bibliography batch. Skip only that candidate; leave source data
        # unchanged for the researcher to correct later.
        try:
            access, identity, cost = tuple(
                float(raw) if raw not in (None, "") else default
                for raw, default in zip(values, defaults)
            )
            candidate = CandidateSource(
                reference_id=reference_id, url=url, source_type=source_type,
                access_confidence=access, identity_confidence=identity,
                expected_cost=cost,
            )
        except (ValueError, TypeError):
            continue
        candidates.append(candidate)
    return candidates


class SourceRegistry:
    """SQLite registry with independent connections for future worker pools."""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS sources (
                    reference_id TEXT NOT NULL,
                    url TEXT NOT NULL,
                    source_type TEXT NOT NULL,
                    discovered_via TEXT NOT NULL,
                    access_confidence REAL NOT NULL,
                    identity_confidence REAL NOT NULL,
                    expected_cost REAL NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    pdf_responses INTEGER NOT NULL DEFAULT 0,
                    verified_downloads INTEGER NOT NULL DEFAULT 0,
                    last_outcome TEXT NOT NULL DEFAULT '',
                    last_attempt_utc TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY(reference_id, url)
                )
            """)
            db.execute("""
                CREATE TABLE IF NOT EXISTS identity_checks (
                    reference_id TEXT NOT NULL,
                    url TEXT NOT NULL,
                    status TEXT NOT NULL,
                    evidence_json TEXT NOT NULL,
                    checked_utc TEXT NOT NULL,
                    PRIMARY KEY (reference_id, url)
                )
            """)

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(str(self.db_path), timeout=30)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def upsert(self, candidate: CandidateSource) -> None:
        """Update metadata without resetting earlier download observations."""
        with self._connect() as db:
            db.execute("""
                INSERT INTO sources (
                    reference_id, url, source_type, discovered_via,
                    access_confidence, identity_confidence, expected_cost
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(reference_id, url) DO UPDATE SET
                    source_type=excluded.source_type,
                    discovered_via=excluded.discovered_via,
                    access_confidence=excluded.access_confidence,
                    identity_confidence=excluded.identity_confidence,
                    expected_cost=excluded.expected_cost
            """, (candidate.reference_id, candidate.url,
                  candidate.source_type, candidate.discovered_via,
                  candidate.access_confidence, candidate.identity_confidence,
                  candidate.expected_cost))

    def import_paper(self, paper: Paper) -> int:
        candidates = candidates_from_paper(paper)
        for candidate in candidates:
            self.upsert(candidate)
        return len(candidates)

    def list_sources(self, reference_id: str) -> List[CandidateSource]:
        with self._connect() as db:
            rows = db.execute("""
                SELECT reference_id, url, source_type, discovered_via,
                       access_confidence, identity_confidence, expected_cost,
                       attempts, pdf_responses, verified_downloads, last_outcome,
                       last_attempt_utc
                  FROM sources WHERE reference_id = ?
            """, (reference_id,)).fetchall()
        sources = [CandidateSource(**dict(row)) for row in rows]
        return sorted(sources, key=lambda candidate: (-candidate.priority_score, candidate.url))

    def record_attempt(self, reference_id: str, url: str, outcome: str) -> None:
        """Record an outcome; only identity-checked PDFs count as verified."""
        if outcome not in OUTCOMES:
            raise ValueError("Unrecognized download outcome")
        with self._connect() as db:
            cur = db.execute("""
                UPDATE sources
                   SET attempts = attempts + 1,
                       pdf_responses = pdf_responses + ?,
                       verified_downloads = verified_downloads + ?,
                       last_outcome = ?, last_attempt_utc = ?
                 WHERE reference_id = ? AND url = ?
            """, (int(outcome in PDF_OUTCOMES), int(outcome == "verified"),
                  outcome, datetime.now(timezone.utc).isoformat(),
                  reference_id, url))
            if cur.rowcount == 0:
                raise KeyError("Candidate source is not registered")

    def record_identity_check(self, reference_id: str, url: str, status: str, evidence: dict) -> None:
        """Persist an assessment and compact evidence next to the source attempt."""
        if status not in {"verified", "needs_review", "mismatch"}:
            raise ValueError("Unknown identity status")
        with self._connect() as db:
            db.execute("""
                INSERT INTO identity_checks
                    (reference_id, url, status, evidence_json, checked_utc)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(reference_id, url) DO UPDATE SET
                    status=excluded.status,
                    evidence_json=excluded.evidence_json,
                    checked_utc=excluded.checked_utc
            """, (reference_id, url, status, json.dumps(evidence, sort_keys=True),
                  datetime.now(timezone.utc).isoformat()))

    def list_identity_checks(self, reference_id: str) -> List[dict]:
        """Return provenance and decisions for all checked sources of a reference."""
        with self._connect() as db:
            rows = db.execute("""
                SELECT url, status, evidence_json, checked_utc
                  FROM identity_checks WHERE reference_id = ?
                 ORDER BY checked_utc, url
            """, (reference_id,)).fetchall()
        return [
            {"url": row["url"], "status": row["status"],
             "evidence": json.loads(row["evidence_json"]), "checked_utc": row["checked_utc"]}
            for row in rows
        ]
