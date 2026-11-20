"""Data ingestion: every configured source, filtered to the lookback window.

One module per kind of source (RSS, sitemap, GitHub releases, Hugging Face, CISA KEV);
each returns the items it kept and one health record per source, and never raises on a
source failure.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from src.config_loader import AppConfig
from src.fetcher.base import RawItem, SourceHealth
from src.fetcher.cisa import fetch_cisa_kev
from src.fetcher.github import fetch_github_releases
from src.fetcher.huggingface import fetch_huggingface_models
from src.fetcher.rss import fetch_rss_feeds
from src.fetcher.sitemap import fetch_sitemaps

__all__ = [
    "RawItem",
    "SourceHealth",
    "fetch_all",
    "fetch_cisa_kev",
    "fetch_github_releases",
    "fetch_huggingface_models",
    "fetch_rss_feeds",
    "fetch_sitemaps",
]

logger = logging.getLogger(__name__)


def fetch_all(
    config: AppConfig, github_token: str | None = None
) -> tuple[list[RawItem], list[SourceHealth]]:
    settings = config.settings
    sources = config.sources
    cutoff = datetime.now(UTC) - timedelta(hours=settings.hours_lookback)
    per_source, chars = settings.max_items_per_source, settings.max_content_chars
    timeout = settings.request_timeout_seconds

    github_items, github_health = fetch_github_releases(
        sources.github_repositories, cutoff, chars, timeout, github_token
    )
    kev_items, kev_health = (
        fetch_cisa_kev(str(sources.cisa_kev_url), cutoff, chars, timeout)
        if sources.cisa_kev_url
        else ([], [])
    )
    rss_items, rss_health = fetch_rss_feeds(sources.rss_feeds, cutoff, per_source, chars, timeout)
    sitemap_items, sitemap_health = fetch_sitemaps(
        sources.sitemaps, cutoff, per_source, chars, timeout
    )
    hf_items, hf_health = fetch_huggingface_models(
        sources.huggingface_authors, cutoff, per_source, chars, timeout
    )
    logger.info(
        "Fetched %d GitHub release(s), %d CISA KEV entry(ies), %d RSS item(s), "
        "%d sitemap page(s) and %d Hugging Face model(s)",
        len(github_items),
        len(kev_items),
        len(rss_items),
        len(sitemap_items),
        len(hf_items),
    )

    # Feeds can overlap (the same article in two feeds): keep the first occurrence of each URL.
    seen: set[str] = set()
    unique: list[RawItem] = []
    fetched = github_items + kev_items + rss_items + sitemap_items + hf_items
    for item in fetched:
        if item["url"] not in seen:
            seen.add(item["url"])
            unique.append(item)
    if len(unique) != len(fetched):
        logger.info("Removed %d duplicate item(s)", len(fetched) - len(unique))
    return unique, github_health + kev_health + rss_health + sitemap_health + hf_health
