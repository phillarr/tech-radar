"""Sites with no RSS feed, read through their sitemap (e.g. Anthropic)."""

from __future__ import annotations

import logging
import re
from datetime import datetime
from html import unescape
from typing import Any
from urllib.parse import urlparse

import requests

from src.config_loader import SitemapSource
from src.fetcher.base import (
    USER_AGENT,
    RawItem,
    SourceHealth,
    clean_text,
    http_get,
    make_health,
    parse_iso8601,
)

logger = logging.getLogger(__name__)

MAX_SITEMAP_PAGES = 30  # pages opened per sitemap and run, newest lastmod first

_LOC_RE = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>\s*(?:<lastmod>\s*([^<\s]+)\s*</lastmod>)?")
_META_RE = re.compile(r"<meta\b[^>]*>", re.IGNORECASE)
_ATTR_RE = re.compile(r'([\w:-]+)\s*=\s*"([^"]*)"')


def _page_meta(html: str) -> dict[str, str]:
    """The <meta> tags of a page, keyed by their property or name attribute."""
    meta: dict[str, str] = {}
    for tag in _META_RE.findall(html):
        attrs = dict(_ATTR_RE.findall(tag))
        key = attrs.get("property") or attrs.get("name")
        if key and "content" in attrs:
            meta.setdefault(key.lower(), unescape(attrs["content"]))
    return meta


def _candidates(
    sitemap_xml: str, host: str | None, sm: SitemapSource, cutoff: datetime
) -> list[tuple[datetime, str]]:
    """Pages modified since the cutoff, newest first. Only pages of the sitemap's own site
    are kept: a tampered sitemap must not make the agent request other hosts (internal
    addresses included) or other schemes."""
    found = []
    for loc, lastmod in _LOC_RE.findall(sitemap_xml):
        modified = parse_iso8601(lastmod)
        if modified is None or modified < cutoff:
            continue
        loc = unescape(loc)
        parts = urlparse(loc)
        if parts.scheme != "https" or parts.hostname != host:
            continue
        if any(parts.path.startswith(prefix) for prefix in sm.include):
            found.append((modified, loc))
    return sorted(found, reverse=True)


def _read_page(
    page_url: str,
    host: str | None,
    sm: SitemapSource,
    cutoff: datetime,
    max_chars: int,
    timeout: float,
) -> tuple[datetime, RawItem] | None:
    try:
        page = http_get(page_url, headers={"User-Agent": USER_AGENT}, timeout=timeout)
        page.raise_for_status()
    except requests.RequestException as exc:
        logger.info("Sitemap page skipped (%s): %s", page_url, exc)
        return None
    if urlparse(page.url).hostname != host:  # redirected to another site
        return None
    meta = _page_meta(page.text)
    published = parse_iso8601(meta.get("article:published_time"))
    if published is None or published.date() < cutoff.date():
        return None
    title = meta.get("og:title") or meta.get("title") or page_url
    description = meta.get("og:description") or meta.get("description") or ""
    return published, RawItem(
        source=sm.name,
        source_type=sm.type,
        title=clean_text(title, 300),
        url=page_url,
        published_at=published.isoformat(),
        content=clean_text(description, max_chars),
    )


def fetch_sitemaps(
    sitemaps: list[SitemapSource],
    cutoff: datetime,
    max_items: int,
    max_chars: int,
    timeout: float,
) -> tuple[list[RawItem], list[SourceHealth]]:
    """A sitemap's lastmod changes whenever a page is edited, sometimes for hundreds of old
    pages at once, so it only picks the candidates: each candidate page is opened and kept
    only if its own publication date (article:published_time) is on or after the cutoff's
    date.
    """
    items: list[RawItem] = []
    health: list[SourceHealth] = []
    for sm in sitemaps:
        sm_url = str(sm.url)

        # The schema's source kind is 'rss' or 'github': a sitemap is the closest to a feed.
        def h(sm: SitemapSource = sm, sm_url: str = sm_url, **kw: Any) -> SourceHealth:
            return make_health(sm.name, sm_url, "rss", sm.type, **kw)

        try:
            resp = http_get(sm_url, headers={"User-Agent": USER_AGENT}, timeout=timeout)
            resp.raise_for_status()
        except requests.RequestException as exc:
            logger.warning("Sitemap fetch failed for %s: %s", sm.name, exc)
            health.append(h(ok=False, error=str(exc)))
            continue

        host = urlparse(sm_url).hostname
        found = []
        for _, page_url in _candidates(resp.text, host, sm, cutoff)[:MAX_SITEMAP_PAGES]:
            page = _read_page(page_url, host, sm, cutoff, max_chars, timeout)
            if page:
                found.append(page)
        found.sort(key=lambda p: p[0], reverse=True)
        selected = [item for _, item in found[: sm.max_items or max_items]]
        items.extend(selected)
        health.append(h(items=len(selected), last=found[0][0] if found else None))
    return items, health
