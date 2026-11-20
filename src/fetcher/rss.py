"""RSS and Atom feeds."""

from __future__ import annotations

import calendar
import logging
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urljoin, urlparse

import feedparser
import requests

from src.config_loader import RSSSource
from src.fetcher.base import USER_AGENT, RawItem, SourceHealth, clean_text, http_get, make_health

logger = logging.getLogger(__name__)


def absolute_url(link: str, site: str) -> str:
    """Some feeds (e.g. HBR) give site-relative links: resolve them against the site."""
    return link if urlparse(link).scheme else urljoin(site, link)


def _entry_datetime(entry: Any) -> datetime | None:
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        struct = entry.get(key)
        if struct:
            return datetime.fromtimestamp(calendar.timegm(struct), tz=UTC)
    return None


def fetch_rss_feeds(
    feeds: list[RSSSource],
    cutoff: datetime,
    max_items: int,
    max_chars: int,
    timeout: float,
) -> tuple[list[RawItem], list[SourceHealth]]:
    items: list[RawItem] = []
    health: list[SourceHealth] = []
    for feed in feeds:
        feed_url = str(feed.url)
        try:
            resp = http_get(feed_url, headers={"User-Agent": USER_AGENT}, timeout=timeout)
            resp.raise_for_status()
        except requests.RequestException as exc:
            logger.warning("RSS fetch failed for %s: %s", feed.name, exc)
            health.append(
                make_health(feed.name, feed_url, "rss", feed.type, ok=False, error=str(exc))
            )
            continue

        parsed = feedparser.parse(resp.content)
        if parsed.bozo and not parsed.entries:
            error = str(parsed.get("bozo_exception"))
            logger.warning("RSS parse failed for %s: %s", feed.name, error)
            health.append(make_health(feed.name, feed_url, "rss", feed.type, ok=False, error=error))
            continue

        dated = [(d, e) for e in parsed.entries if (d := _entry_datetime(e)) is not None]
        recent = sorted(((d, e) for d, e in dated if d >= cutoff), key=lambda p: p[0], reverse=True)
        selected = recent[: feed.max_items or max_items]
        newest = max((d for d, _ in dated), default=None)
        health.append(
            make_health(feed.name, feed_url, "rss", feed.type, items=len(selected), last=newest)
        )

        site = parsed.feed.get("link") or feed_url
        for published, entry in selected:
            content = entry.get("summary") or ""
            if not content and entry.get("content"):
                content = entry["content"][0].get("value", "")
            items.append(
                RawItem(
                    source=feed.name,
                    source_type=feed.type,
                    title=clean_text(entry.get("title", "Untitled"), 300),
                    url=absolute_url(entry.get("link") or feed_url, site),
                    published_at=published.isoformat(),
                    content=clean_text(content, max_chars),
                )
            )
    return items, health
