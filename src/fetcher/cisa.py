"""CISA's Known Exploited Vulnerabilities catalog."""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime
from typing import Any

import requests

from src.fetcher.base import USER_AGENT, RawItem, SourceHealth, clean_text, http_get, make_health

logger = logging.getLogger(__name__)

KEV_NAME = "CISA KEV"
KEV_PAGE = "https://www.cisa.gov/known-exploited-vulnerabilities-catalog"


def fetch_cisa_kev(
    url: str, cutoff: datetime, max_chars: int, timeout: float
) -> tuple[list[RawItem], list[SourceHealth]]:
    """CVEs with evidence of active exploitation. One JSON file (no RSS); entries only carry
    the date they were added, so every entry added on or after the cutoff's date is kept,
    and the cross-day history drops those already published. Read from CISA's own GitHub
    repository because cisa.gov refuses requests from GitHub Actions.
    """

    # The schema's source kind is 'rss' or 'github': this file is served from GitHub.
    def health(**kw: Any) -> SourceHealth:
        return make_health(KEV_NAME, KEV_PAGE, "github", "OFFICIAL", **kw)

    try:
        resp = http_get(url, headers={"User-Agent": USER_AGENT}, timeout=timeout)
        resp.raise_for_status()
        entries = resp.json()["vulnerabilities"]
    except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
        logger.warning("CISA KEV fetch failed: %s", exc)
        return [], [health(ok=False, error=str(exc))]

    items: list[RawItem] = []
    newest: date | None = None
    for entry in entries:
        try:
            added = date.fromisoformat(entry["dateAdded"])
            cve = entry["cveID"]
        except (KeyError, TypeError, ValueError):
            continue
        newest = max(newest or added, added)
        if added < cutoff.date():
            continue
        ransomware = entry.get("knownRansomwareCampaignUse") or "Unknown"
        name = entry.get("vulnerabilityName") or "exploited vulnerability"
        content = (
            f"CISA added {cve} to its Known Exploited Vulnerabilities catalog on {added}: "
            f"there is evidence of active exploitation. "
            f"Vendor: {entry.get('vendorProject', '')}. Product: {entry.get('product', '')}. "
            f"{entry.get('shortDescription', '')} "
            f"Known use in ransomware campaigns: {ransomware}. "
            f"Required action: {entry.get('requiredAction', '')}"
        )
        items.append(
            RawItem(
                source=KEV_NAME,
                source_type="OFFICIAL",
                title=clean_text(f"{cve}: {name}", 300),
                url=f"https://nvd.nist.gov/vuln/detail/{cve}",
                published_at=added.isoformat(),  # a date: CISA gives no time of day
                content=clean_text(content, max_chars),
            )
        )
    last = datetime(newest.year, newest.month, newest.day, tzinfo=UTC) if newest else None
    return items, [health(items=len(items), last=last)]
