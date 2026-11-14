"""Data ingestion: GitHub releases and RSS feeds, filtered to the lookback window."""

from __future__ import annotations

import calendar
import logging
import re
from datetime import datetime, timedelta, timezone
from html import unescape
from typing import List, Optional, TypedDict

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
    title: str
    url: str
    content: str


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
) -> List[RawItem]:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": USER_AGENT,
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    items: List[RawItem] = []
    for repo in repos:
        url = f"{GITHUB_API}/repos/{repo}/releases/latest"
        try:
            resp = requests.get(url, headers=headers, timeout=timeout)
            if resp.status_code == 404:
                logger.info("No published release for %s", repo)
                continue
            resp.raise_for_status()
            release = resp.json()
        except (requests.RequestException, ValueError) as exc:
            logger.warning("GitHub fetch failed for %s: %s", repo, exc)
            continue

        published = _parse_iso8601(release.get("published_at"))
        if published is None or published < cutoff:
            continue

        name = release.get("name") or release.get("tag_name") or "New release"
        items.append(
            RawItem(
                source=f"GitHub · {repo}",
                title=f"{repo} {name}".strip(),
                url=release.get("html_url") or f"https://github.com/{repo}/releases",
                content=_clean_text(release.get("body") or "", max_chars),
            )
        )
    return items


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
) -> List[RawItem]:
    items: List[RawItem] = []
    for feed in feeds:
        try:
            resp = requests.get(str(feed.url), headers={"User-Agent": USER_AGENT}, timeout=timeout)
            resp.raise_for_status()
        except requests.RequestException as exc:
            logger.warning("RSS fetch failed for %s: %s", feed.name, exc)
            continue

        parsed = feedparser.parse(resp.content)
        if parsed.bozo and not parsed.entries:
            logger.warning("RSS parse failed for %s: %s", feed.name, parsed.get("bozo_exception"))
            continue

        recent = []
        for entry in parsed.entries:
            published = _entry_datetime(entry)
            if published is None or published < cutoff:
                continue
            recent.append((published, entry))

        recent.sort(key=lambda pair: pair[0], reverse=True)
        for _, entry in recent[: feed.max_items or max_items]:
            content = entry.get("summary") or ""
            if not content and entry.get("content"):
                content = entry["content"][0].get("value", "")
            items.append(
                RawItem(
                    source=feed.name,
                    title=_clean_text(entry.get("title", "Untitled"), 300),
                    url=entry.get("link", str(feed.url)),
                    content=_clean_text(content, max_chars),
                )
            )
    return items


def fetch_all(config: AppConfig, github_token: Optional[str] = None) -> List[RawItem]:
    settings = config.settings
    cutoff = datetime.now(timezone.utc) - timedelta(hours=settings.hours_lookback)

    github_items = fetch_github_releases(
        config.sources.github_repositories,
        cutoff,
        settings.max_content_chars,
        settings.request_timeout_seconds,
        github_token,
    )
    rss_items = fetch_rss_feeds(
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
    return unique
