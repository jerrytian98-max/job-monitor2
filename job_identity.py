"""Stable job identities used for scraping, persistence, and notification deduplication."""

from __future__ import annotations

import hashlib
import re
from typing import Mapping
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


_TRACKING_QUERY_PARAMS = {
    "track_id",
    "trackid",
    "tracking_id",
    "trackingid",
    "spm",
    "scm",
}


def canonicalize_job_url(value: object) -> str:
    """Remove non-semantic tracking data while retaining the job detail identity.

    Alibaba recruitment sites attach a fresh ``track_id`` to the same
    ``positionId`` on each query.  Keeping it would make one job look new on
    every run.  The function intentionally preserves meaningful query values
    such as ``positionId`` and URL fragments used by hash-routed job sites.
    """
    raw_url = str(value or "").strip()
    if not raw_url:
        return ""

    parsed = urlsplit(raw_url)
    if not parsed.scheme or not parsed.netloc:
        return raw_url

    query_items = [
        (key, item_value)
        for key, item_value in parse_qsl(parsed.query, keep_blank_values=True)
        if not _is_tracking_parameter(key)
    ]
    query_items.sort(key=lambda item: (item[0], item[1]))
    return urlunsplit(
        (
            parsed.scheme.lower(),
            parsed.netloc.lower(),
            parsed.path,
            urlencode(query_items, doseq=True),
            parsed.fragment,
        )
    )


def build_job_identity(job: Mapping[str, object]) -> str:
    """Build a repeatable identity without volatile URL tracking parameters."""
    canonical_url = canonicalize_job_url(job.get("url"))
    title = _normalize_text(job.get("title"))
    if canonical_url:
        return f"url:{canonical_url}|title:{title}"

    company = _normalize_text(job.get("company"))
    return f"title:{company}|{title}"


def job_identity_hash(job: Mapping[str, object]) -> str:
    return hashlib.md5(build_job_identity(job).encode("utf-8")).hexdigest()


def _is_tracking_parameter(key: object) -> bool:
    normalized = str(key or "").strip().lower()
    return normalized in _TRACKING_QUERY_PARAMS or normalized.startswith("utm_")


def _normalize_text(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()
