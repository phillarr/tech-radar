"""What every source fetcher shares: the item and health records, the size-capped HTTP
download, and text and date helpers."""

from __future__ import annotations

import re
from datetime import datetime
from html import unescape
from typing import Any, NotRequired, TypedDict

import requests

USER_AGENT = "tech-watch-agent/1.0"
MAX_RESPONSE_BYTES = 10_000_000  # a feed, sitemap or page larger than this is refused

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
    kind: str  # rss | github (the database schema's two kinds; see each fetcher)
    type: str
    ok: bool
    error: str | None
    items: int
    last_item_at: str | None  # newest item seen in the source, ISO 8601 UTC


def make_health(
    name: str,
    url: str,
    kind: str,
    type_: str,
    ok: bool = True,
    error: str | None = None,
    items: int = 0,
    last: datetime | None = None,
) -> SourceHealth:
    return SourceHealth(
        name=name,
        url=url,
        kind=kind,
        type=type_,
        ok=ok,
        error=error,
        items=items,
        last_item_at=last.isoformat() if last else None,
    )


class ResponseTooLarge(requests.RequestException):
    pass


def http_get(url: str, **kwargs: Any) -> requests.Response:
    """requests.get with a size cap, so a broken or hostile source cannot exhaust memory."""
    resp = requests.get(url, stream=True, **kwargs)
    try:
        declared = resp.headers.get("Content-Length")
        if declared and declared.isdigit() and int(declared) > MAX_RESPONSE_BYTES:
            raise ResponseTooLarge(f"response larger than {MAX_RESPONSE_BYTES} bytes")
        body = b""
        for chunk in resp.iter_content(64 * 1024):
            body += chunk
            if len(body) > MAX_RESPONSE_BYTES:
                raise ResponseTooLarge(f"response larger than {MAX_RESPONSE_BYTES} bytes")
        resp._content = body
    finally:
        resp.close()
    return resp


def clean_text(text: str, max_chars: int) -> str:
    """Plain text from HTML: tags removed, entities decoded, whitespace collapsed, capped."""
    text = _WS_RE.sub(" ", unescape(_TAG_RE.sub(" ", text or ""))).strip()
    return text if len(text) <= max_chars else text[:max_chars].rstrip() + "…"


def parse_iso8601(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
