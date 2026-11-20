"""Shared fixtures: items and model outputs reproducing the 14 November 2026 Slack
digests, and HTTP responses for the fetchers (no network, no API call)."""

from __future__ import annotations

import io
import json
import sys
import types
from datetime import UTC, datetime

import requests

try:  # the analyzer imports the SDK at module level; tests never call the API
    import anthropic  # noqa: F401
except ImportError:
    sys.modules["anthropic"] = types.ModuleType("anthropic")

from src.analyzer import (  # noqa: E402
    AnalysisOutput,
    Event,
    EventSource,
    RecommendedAction,
    Scores,
    postprocess,
)

NOW = datetime(2026, 11, 14, 7, 0, tzinfo=UTC)


def response(text: str = "", json_body=None, url: str = "", status: int = 200, headers=None):
    """A real requests.Response, readable by the fetchers' size-capped download."""
    body = json.dumps(json_body) if json_body is not None else text
    r = requests.Response()
    r.status_code, r.url, r.encoding = status, url, "utf-8"
    r.raw = io.BytesIO(body.encode("utf-8"))
    r.headers.update(headers or {})
    return r


def item(url, source, source_type, title="t"):
    return {
        "source": source,
        "source_type": source_type,
        "title": title,
        "url": url,
        "published_at": "2026-11-13T18:00:00+00:00",
        "content": "",
    }


ITEMS = [
    item("https://openai.com/index/introducing-chatgpt-wallet", "OpenAI News", "OFFICIAL"),
    item("https://www.theverge.com/openai-chatgpt-wallet", "The Verge AI", "MEDIA"),
    item("https://techcrunch.com/chatgpt-wallet", "TechCrunch AI", "MEDIA"),
    item("https://github.com/ggml-org/ggml/releases/tag/v1.0.0", "Hacker News - Best", "COMMUNITY"),
    item("https://www.githubstatus.com/incidents/51drrqvsv66g", "Status · GitHub", "OFFICIAL"),
    item("https://status.claude.com/incidents/sqn3fg1l96dl", "Status · Claude", "OFFICIAL"),
    item("https://www.wired.com/story/microsoft-365-outage", "WIRED AI", "MEDIA"),
    item("https://techcrunch.com/langchain-3b", "TechCrunch AI", "MEDIA"),
]


def event(title, sources, **overrides):
    base = dict(
        title=title,
        category="AI_PRODUCT_MANAGEMENT",
        secondary_categories=[],
        event_type="PRODUCT_LAUNCH",
        evidence_status="CONFIRMED",
        sources=[EventSource(url=u, role=r) for u, r in sources],
        facts=["fact"],
        summary="What happened.",
        why_it_matters="Why it matters.",
        product_implications=[],
        engineering_implications=[],
        recommended_actions=[
            RecommendedAction(action="Read the docs.", type="INVESTIGATE", timeframe="THIS_WEEK")
        ],
        product_opportunity=None,
        uncertainties=[],
        scores=Scores(relevance=70, impact=60, urgency=25),
        priority="INFO",
    )
    base.update(overrides)
    return Event(**base)


def run(events, signals=(), max_events=10):
    output = AnalysisOutput(intro="Intro.", events=list(events), signals=list(signals))
    return postprocess(output, ITEMS, max_events, now=NOW)
