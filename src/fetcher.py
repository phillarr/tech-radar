"""Data ingestion: GitHub releases and RSS feeds, filtered to the lookback window."""

from __future__ import annotations

import calendar
import logging
import re
from datetime import date, datetime, timedelta, timezone
from html import unescape
from urllib.parse import urljoin, urlparse
from typing import List, NotRequired, Optional, Tuple, TypedDict

import feedparser
import requests

from src.config_loader import AppConfig, HuggingFaceAuthor, RSSSource, SitemapSource

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


def _absolute(link: str, site: str) -> str:
    """Some feeds (e.g. HBR) give site-relative links: resolve them against the site."""
    return link if urlparse(link).scheme else urljoin(site, link)


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

        release_name = release.get("name") or release.get("tag_name") or "New release"
        items.append(
            RawItem(
                source=name,  # "GitHub · owner/repo", not the release name
                source_type="OFFICIAL",
                title=f"{repo} {release_name}".strip(),
                url=release.get("html_url") or f"https://github.com/{repo}/releases",
                published_at=published.isoformat(),
                content=_clean_text(release.get("body") or "", max_chars),
            )
        )
    return items, health


KEV_NAME = "CISA KEV"
KEV_PAGE = "https://www.cisa.gov/known-exploited-vulnerabilities-catalog"


def fetch_cisa_kev(
    url: str, cutoff: datetime, max_chars: int, timeout: float
) -> Tuple[List[RawItem], List[SourceHealth]]:
    """CISA's Known Exploited Vulnerabilities catalog: CVEs with evidence of active
    exploitation. One JSON file (no RSS); entries only carry the date they were added,
    so every entry added on or after the cutoff's date is kept, and the cross-day history
    drops those already published. Read from CISA's own GitHub repository because
    cisa.gov refuses requests from GitHub Actions.
    """
    # The schema's source kind is 'rss' or 'github': this file is served from GitHub.
    def health(**kw) -> SourceHealth:
        return _health(KEV_NAME, KEV_PAGE, "github", "OFFICIAL", **kw)

    try:
        resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=timeout)
        resp.raise_for_status()
        entries = resp.json()["vulnerabilities"]
    except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
        logger.warning("CISA KEV fetch failed: %s", exc)
        return [], [health(ok=False, error=str(exc))]

    items: List[RawItem] = []
    newest: Optional[date] = None
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
                title=_clean_text(f"{cve}: {name}", 300),
                url=f"https://nvd.nist.gov/vuln/detail/{cve}",
                published_at=added.isoformat(),  # a date: CISA gives no time of day
                content=_clean_text(content, max_chars),
            )
        )
    last = datetime(newest.year, newest.month, newest.day, tzinfo=timezone.utc) if newest else None
    return items, [health(items=len(items), last=last)]


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

        site = parsed.feed.get("link") or feed_url
        for published, entry in selected:
            content = entry.get("summary") or ""
            if not content and entry.get("content"):
                content = entry["content"][0].get("value", "")
            items.append(
                RawItem(
                    source=feed.name,
                    source_type=feed.type,
                    title=_clean_text(entry.get("title", "Untitled"), 300),
                    url=_absolute(entry.get("link") or feed_url, site),
                    published_at=published.isoformat(),
                    content=_clean_text(content, max_chars),
                )
            )
    return items, health


_LOC_RE = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>\s*(?:<lastmod>\s*([^<\s]+)\s*</lastmod>)?")
_META_RE = re.compile(r"<meta\b[^>]*>", re.IGNORECASE)
_ATTR_RE = re.compile(r'([\w:-]+)\s*=\s*"([^"]*)"')
MAX_SITEMAP_PAGES = 30  # pages opened per sitemap and run, newest lastmod first


def _page_meta(html: str) -> dict:
    """The <meta> tags of a page, keyed by their property or name attribute."""
    meta = {}
    for tag in _META_RE.findall(html):
        attrs = dict(_ATTR_RE.findall(tag))
        key = attrs.get("property") or attrs.get("name")
        if key and "content" in attrs:
            meta.setdefault(key.lower(), unescape(attrs["content"]))
    return meta


def fetch_sitemaps(
    sitemaps: List[SitemapSource],
    cutoff: datetime,
    max_items: int,
    max_chars: int,
    timeout: float,
) -> Tuple[List[RawItem], List[SourceHealth]]:
    """Sites with no RSS feed (e.g. Anthropic). A sitemap's lastmod changes whenever a page
    is edited, sometimes for hundreds of old pages at once, so it only picks the candidates:
    each candidate page is opened and kept only if its own publication date
    (article:published_time) is on or after the cutoff's date.
    """
    items: List[RawItem] = []
    health: List[SourceHealth] = []
    for sm in sitemaps:
        sm_url = str(sm.url)

        # The schema's source kind is 'rss' or 'github': a sitemap is the closest to a feed.
        def h(**kw) -> SourceHealth:
            return _health(sm.name, sm_url, "rss", sm.type, **kw)

        try:
            resp = requests.get(sm_url, headers={"User-Agent": USER_AGENT}, timeout=timeout)
            resp.raise_for_status()
        except requests.RequestException as exc:
            logger.warning("Sitemap fetch failed for %s: %s", sm.name, exc)
            health.append(h(ok=False, error=str(exc)))
            continue

        candidates = []
        for loc, lastmod in _LOC_RE.findall(resp.text):
            modified = _parse_iso8601(lastmod)
            if modified is None or modified < cutoff:
                continue
            loc = unescape(loc)
            if any(urlparse(loc).path.startswith(prefix) for prefix in sm.include):
                candidates.append((modified, loc))
        candidates.sort(reverse=True)

        found: List[Tuple[datetime, RawItem]] = []
        for _, page_url in candidates[:MAX_SITEMAP_PAGES]:
            try:
                page = requests.get(page_url, headers={"User-Agent": USER_AGENT}, timeout=timeout)
                page.raise_for_status()
            except requests.RequestException as exc:
                logger.info("Sitemap page skipped (%s): %s", page_url, exc)
                continue
            meta = _page_meta(page.text)
            published = _parse_iso8601(meta.get("article:published_time"))
            if published is None or published.date() < cutoff.date():
                continue
            title = meta.get("og:title") or meta.get("title") or page_url
            description = meta.get("og:description") or meta.get("description") or ""
            found.append((published, RawItem(
                source=sm.name,
                source_type=sm.type,
                title=_clean_text(title, 300),
                url=page_url,
                published_at=published.isoformat(),
                content=_clean_text(description, max_chars),
            )))
        found.sort(key=lambda p: p[0], reverse=True)
        selected = [item for _, item in found[: sm.max_items or max_items]]
        items.extend(selected)
        health.append(h(items=len(selected), last=found[0][0] if found else None))
    return items, health


HF_API = "https://huggingface.co/api/models"
_FRONT_MATTER_RE = re.compile(r"\A---\s*\n.*?\n---\s*\n", re.DOTALL)
_MD_IMAGE_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_MD_LINK_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")


def _model_card_text(card: str) -> str:
    card = _FRONT_MATTER_RE.sub("", card)
    return _MD_LINK_RE.sub(r"\1", _MD_IMAGE_RE.sub(" ", card)).replace("#", " ")


def fetch_huggingface_models(
    authors: List[HuggingFaceAuthor],
    cutoff: datetime,
    max_items: int,
    max_chars: int,
    timeout: float,
) -> Tuple[List[RawItem], List[SourceHealth]]:
    """New models published by a lab's own Hugging Face organization (e.g. DeepSeek, Qwen):
    for labs with no feed, it is the official release channel of their open models.
    """
    headers = {"User-Agent": USER_AGENT}
    items: List[RawItem] = []
    health: List[SourceHealth] = []
    for org in authors:
        name, page = f"Hugging Face · {org.name}", f"https://huggingface.co/{org.author}"
        # The schema's source kind is 'rss' or 'github': a model listing is closest to a feed.
        try:
            resp = requests.get(
                HF_API, headers=headers, timeout=timeout,
                params={"author": org.author, "sort": "createdAt", "direction": "-1",
                        "limit": str(max_items * 2)},
            )
            resp.raise_for_status()
            models = resp.json()
            if not isinstance(models, list):
                raise ValueError("unexpected response")
        except (requests.RequestException, ValueError) as exc:
            logger.warning("Hugging Face fetch failed for %s: %s", org.author, exc)
            health.append(_health(name, page, "rss", "OFFICIAL", ok=False, error=str(exc)))
            continue

        dated = [(d, m) for m in models
                 if isinstance(m, dict) and m.get("id")
                 and (d := _parse_iso8601(m.get("createdAt"))) is not None]
        recent = sorted(((d, m) for d, m in dated if d >= cutoff), key=lambda p: p[0], reverse=True)
        selected = recent[:max_items]
        newest = max((d for d, _ in dated), default=None)
        health.append(_health(name, page, "rss", "OFFICIAL", items=len(selected), last=newest))

        for created, model in selected:
            model_id = model["id"]
            card = ""
            try:
                card_resp = requests.get(f"https://huggingface.co/{model_id}/raw/main/README.md",
                                         headers=headers, timeout=timeout)
                if card_resp.ok:
                    card = _model_card_text(card_resp.text)
            except requests.RequestException as exc:
                logger.info("No model card for %s: %s", model_id, exc)
            task = model.get("pipeline_tag") or "unspecified task"
            content = (f"{org.name} published a new model on its official Hugging Face page: "
                       f"{model_id} ({task}). {card}")
            items.append(
                RawItem(
                    source=name,
                    source_type="OFFICIAL",
                    title=f"{org.name} releases {model_id.split('/', 1)[-1]} on Hugging Face",
                    url=f"https://huggingface.co/{model_id}",
                    published_at=created.isoformat(),
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
    sitemap_items, sitemap_health = fetch_sitemaps(
        config.sources.sitemaps,
        cutoff,
        settings.max_items_per_source,
        settings.max_content_chars,
        settings.request_timeout_seconds,
    )
    hf_items, hf_health = fetch_huggingface_models(
        config.sources.huggingface_authors,
        cutoff,
        settings.max_items_per_source,
        settings.max_content_chars,
        settings.request_timeout_seconds,
    )
    kev_items, kev_health = (
        fetch_cisa_kev(str(config.sources.cisa_kev_url), cutoff, settings.max_content_chars,
                       settings.request_timeout_seconds)
        if config.sources.cisa_kev_url else ([], [])
    )
    logger.info("Fetched %d GitHub release(s), %d CISA KEV entry(ies), %d RSS item(s), "
                "%d sitemap page(s) and %d Hugging Face model(s)", len(github_items),
                len(kev_items), len(rss_items), len(sitemap_items), len(hf_items))

    # Feeds can overlap (the same article in two feeds): keep the first occurrence of each URL.
    seen: set[str] = set()
    unique: List[RawItem] = []
    fetched = github_items + kev_items + rss_items + sitemap_items + hf_items
    for item in fetched:
        if item["url"] not in seen:
            seen.add(item["url"])
            unique.append(item)
    if len(unique) != len(fetched):
        logger.info("Removed %d duplicate item(s)", len(fetched) - len(unique))
    return unique, github_health + kev_health + rss_health + sitemap_health + hf_health
