"""Data ingestion: GitHub releases and RSS feeds, filtered to the lookback window."""

from __future__ import annotations

import calendar
import logging
import re
from datetime import datetime, timedelta, timezone
from html import unescape
from typing import List, NotRequired, Optional, Tuple, TypedDict

import feedparser
import requests

from src.config_loader import AppConfig, RSSSource

logger = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"
USER_AGENT = "tech-watch-agent/1.0"
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


class RawItem(TypedDict):
    source: str
    source_type: str  # OFFICIAL | MEDIA | RESEARCH | COMMUNITY
    title: str
    url: str
    published_at: str  # ISO 8601, UTC
    content: str
    update: NotRequired[bool]  # set when this URL was already published with other content


class SourceHealth(TypedDict):
    name: str
    url: str
    kind: str  # rss | github
    type: str
    ok: bool
    error: Optional[str]
    items: int
    last_item_at: Optional[str]  # newest item seen in the feed, ISO 8601 UTC


def _health(name, url, kind, type_, ok=True, error=None, items=0, last=None) -> SourceHealth:
    return SourceHealth(name=name, url=url, kind=kind, type=type_, ok=ok, error=error,
                        items=items, last_item_at=last.isoformat() if last else None)


def _clean_text(text: str, max_chars: int) -> str:
    text = _WS_RE.sub(" ", unescape(_TAG_RE.sub(" ", text or ""))).strip()
    return text if len(text) <= max_chars else text[:max_chars].rstrip() + "…"


def _parse_iso8601(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def fetch_github_releases(
    repos: List[str],
    cutoff: datetime,
    max_chars: int,
    timeout: float,
    token: Optional[str] = None,
) -> Tuple[List[RawItem], List[SourceHealth]]:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": USER_AGENT,
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    items: List[RawItem] = []
    health: List[SourceHealth] = []
    for repo in repos:
        name, page = f"GitHub · {repo}", f"https://github.com/{repo}/releases"
        url = f"{GITHUB_API}/repos/{repo}/releases/latest"
        try:
            resp = requests.get(url, headers=headers, timeout=timeout)
            if resp.status_code == 404:
                logger.info("No published release for %s", repo)
                health.append(_health(name, page, "github", "OFFICIAL"))
                continue
            resp.raise_for_status()
            release = resp.json()
        except (requests.RequestException, ValueError) as exc:
            logger.warning("GitHub fetch failed for %s: %s", repo, exc)
            health.append(_health(name, page, "github", "OFFICIAL", ok=False, error=str(exc)))
            continue

        published = _parse_iso8601(release.get("published_at"))
        fresh = published is not None and published >= cutoff
        health.append(_health(name, page, "github", "OFFICIAL", items=int(fresh), last=published))
        if not fresh:
            continue

        name = release.get("name") or release.get("tag_name") or "New release"
        items.append(
            RawItem(
                source=name,
                source_type="OFFICIAL",
                title=f"{repo} {name}".strip(),
                url=release.get("html_url") or f"https://github.com/{repo}/releases",
                published_at=published.isoformat(),
                content=_clean_text(release.get("body") or "", max_chars),
            )
        )
    return items, health


def _entry_datetime(entry: feedparser.FeedParserDict) -> Optional[datetime]:
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        struct = entry.get(key)
        if struct:
            return datetime.fromtimestamp(calendar.timegm(struct), tz=timezone.utc)
    return None


def fetch_rss_feeds(
    feeds: List[RSSSource],
    cutoff: datetime,
    max_items: int,
    max_chars: int,
    timeout: float,
) -> Tuple[List[RawItem], List[SourceHealth]]:
    items: List[RawItem] = []
    health: List[SourceHealth] = []
    for feed in feeds:
        feed_url = str(feed.url)
        try:
            resp = requests.get(feed_url, headers={"User-Agent": USER_AGENT}, timeout=timeout)
            resp.raise_for_status()
        except requests.RequestException as exc:
            logger.warning("RSS fetch failed for %s: %s", feed.name, exc)
            health.append(_health(feed.name, feed_url, "rss", feed.type, ok=False, error=str(exc)))
            continue

        parsed = feedparser.parse(resp.content)
        if parsed.bozo and not parsed.entries:
            error = str(parsed.get("bozo_exception"))
            logger.warning("RSS parse failed for %s: %s", feed.name, error)
            health.append(_health(feed.name, feed_url, "rss", feed.type, ok=False, error=error))
            continue

        dated = [(d, e) for e in parsed.entries if (d := _entry_datetime(e)) is not None]
        recent = sorted(((d, e) for d, e in dated if d >= cutoff), key=lambda p: p[0], reverse=True)
        selected = recent[: feed.max_items or max_items]
        newest = max((d for d, _ in dated), default=None)
        health.append(_health(feed.name, feed_url, "rss", feed.type, items=len(selected), last=newest))

        for published, entry in selected:
            content = entry.get("summary") or ""
            if not content and entry.get("content"):
                content = entry["content"][0].get("value", "")
            items.append(
                RawItem(
                    source=feed.name,
                    source_type=feed.type,
                    title=_clean_text(entry.get("title", "Untitled"), 300),
                    url=entry.get("link", str(feed.url)),
                    published_at=published.isoformat(),
                    content=_clean_text(content, max_chars),
                )
            )
    return items, health


def fetch_all(
    config: AppConfig, github_token: Optional[str] = None
) -> Tuple[List[RawItem], List[SourceHealth]]:
    settings = config.settings
    cutoff = datetime.now(timezone.utc) - timedelta(hours=settings.hours_lookback)

    github_items, github_health = fetch_github_releases(
        config.sources.github_repositories,
        cutoff,
        settings.max_content_chars,
        settings.request_timeout_seconds,
        github_token,
    )
    rss_items, rss_health = fetch_rss_feeds(
        config.sources.rss_feeds,
        cutoff,
        settings.max_items_per_source,
        settings.max_content_chars,
        settings.request_timeout_seconds,
    )
    logger.info("Fetched %d GitHub release(s) and %d RSS item(s)", len(github_items), len(rss_items))

    # Feeds overlap (e.g. HN Best vs HN 200+ points): keep the first occurrence of each URL.
    seen: set[str] = set()
    unique: List[RawItem] = []
    for item in github_items + rss_items:
        if item["url"] not in seen:
            seen.add(item["url"])
            unique.append(item)
    if len(unique) != len(github_items) + len(rss_items):
        logger.info("Removed %d duplicate item(s)", len(github_items) + len(rss_items) - len(unique))
    return unique, github_health + rss_health
