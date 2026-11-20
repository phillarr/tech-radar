"""Latest release of each tracked GitHub repository."""

from __future__ import annotations

import logging
from datetime import datetime

import requests

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

GITHUB_API = "https://api.github.com"


def fetch_github_releases(
    repos: list[str],
    cutoff: datetime,
    max_chars: int,
    timeout: float,
    token: str | None = None,
) -> tuple[list[RawItem], list[SourceHealth]]:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": USER_AGENT,
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    items: list[RawItem] = []
    health: list[SourceHealth] = []
    for repo in repos:
        name, page = f"GitHub · {repo}", f"https://github.com/{repo}/releases"
        url = f"{GITHUB_API}/repos/{repo}/releases/latest"
        try:
            resp = http_get(url, headers=headers, timeout=timeout)
            if resp.status_code == 404:
                logger.info("No published release for %s", repo)
                health.append(make_health(name, page, "github", "OFFICIAL"))
                continue
            resp.raise_for_status()
            release = resp.json()
        except (requests.RequestException, ValueError) as exc:
            logger.warning("GitHub fetch failed for %s: %s", repo, exc)
            health.append(make_health(name, page, "github", "OFFICIAL", ok=False, error=str(exc)))
            continue

        published = parse_iso8601(release.get("published_at"))
        fresh = published is not None and published >= cutoff
        health.append(
            make_health(name, page, "github", "OFFICIAL", items=int(fresh), last=published)
        )
        if published is None or not fresh:
            continue

        release_name = release.get("name") or release.get("tag_name") or "New release"
        items.append(
            RawItem(
                source=name,  # "GitHub · owner/repo", not the release name
                source_type="OFFICIAL",
                title=f"{repo} {release_name}".strip(),
                url=release.get("html_url") or page,
                published_at=published.isoformat(),
                content=clean_text(release.get("body") or "", max_chars),
            )
        )
    return items, health
