"""Slack Block Kit message builder and Incoming Webhook sender."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import requests

from src.analyzer import DailyReport, IntelligenceEvent, Signal

logger = logging.getLogger(__name__)

BADGES = {
    "CRITICAL": "🔴 [CRITICAL]",
    "MAJOR": "🟡 [MAJOR]",
    "INFO": "🔵 [INFO]",
}
CATEGORIES = {
    "CYBERSECURITY": "🛡️ Cybersecurity",
    "AI_PRODUCT_MANAGEMENT": "🧭 AI Product Management",
    "AI_ENGINEERING": "🛠️ AI Engineering",
    "CLOUD_INFRASTRUCTURE": "☁️ Cloud & Infrastructure",
    "TECH_STARTUPS": "🚀 Tech & Startups",
    "RESEARCH_INNOVATION": "🔬 Research & Innovation",
    "AI_GOVERNANCE": "⚖️ AI Governance",
}
SOURCE_TYPES = {
    "OFFICIAL": "official",
    "MEDIA": "media",
    "RESEARCH": "research",
    "COMMUNITY": "community",
}
EVIDENCE = {
    "CONFIRMED": "✅ officially confirmed",
    "REPORTED": "📰 press report, not officially confirmed",
    "UNVERIFIED": "❔ unverified",
}
# Footer legend, for readers who do not know the evidence statuses.
EVIDENCE_LEGEND = (
    "✅ officially confirmed: stated by the company, project or authority itself · "
    "📰 press report: covered by the media only, not yet confirmed by those concerned"
)
AI_NOTICE = (
    "🤖 Summaries, priorities and recommendations are AI-generated and may contain errors: "
    "check the source before acting."
)
TIMEFRAMES = {
    "NOW": "now",
    "THIS_WEEK": "this week",
    "THIS_MONTH": "this month",
    "LATER": "later",
}
SIGNAL_LEVELS = {"WEAK_SIGNAL": "weak signal", "CORROBORATED": "corroborated"}

# Slack limits: 50 blocks per message, 3000 chars per section text.
MAX_BLOCKS = 50
MAX_SECTION_CHARS = 3000


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _link(url: str, label: str) -> str:
    """A Slack link. Feed URLs are untrusted: only http(s) links are kept, and characters
    that would end the link markup early (and could inject a mention) are escaped."""
    text = _escape(label).replace("|", "¦")
    if urlparse(url).scheme not in ("http", "https"):
        return text
    return f"<{_escape(url).replace('|', '%7C')}|{text}>"


def _section(text: str) -> Dict[str, Any]:
    return {"type": "section", "text": {"type": "mrkdwn", "text": text[:MAX_SECTION_CHARS]}}


def _context(text: str) -> Dict[str, Any]:
    return {"type": "context", "elements": [{"type": "mrkdwn", "text": text}]}


def _local_time(iso: str, tz: ZoneInfo) -> str:
    try:
        if len(iso) == 10:  # a date without a time of day (CISA KEV)
            return datetime.fromisoformat(iso).strftime("%d %b")
        return datetime.fromisoformat(iso).astimezone(tz).strftime("%d %b %H:%M")
    except ValueError:
        return ""


def _event_block(event: IntelligenceEvent, tz: ZoneInfo) -> Dict[str, Any]:
    primary = event.sources[0]
    lines = [
        f"*{BADGES[event.priority]}* {_link(primary.url, event.title)}",
        f"*What happened:* {_escape(event.summary)}",
        f"*Why it matters:* {_escape(event.why_it_matters)}",
    ]
    if event.recommended_actions:
        action = event.recommended_actions[0]
        lines.append(
            f"*Recommended action:* {_escape(action.action)} _({TIMEFRAMES[action.timeframe]})_"
        )
    if event.product_opportunity:
        lines.append(f"*Product opportunity:* {_escape(event.product_opportunity)}")

    others = " ".join(_link(s.url, f"[{i}]") for i, s in enumerate(event.sources[1:], start=2))
    n = len(event.sources)
    trace = (
        f"_Source: {_escape(primary.name)} ({SOURCE_TYPES.get(primary.type, primary.type)}) · "
        f"{n} source{'s' if n > 1 else ''} · {EVIDENCE[event.evidence_status]} · "
        f"{_local_time(primary.published_at, tz)}_"
    )
    lines.append(f"{trace} {others}".rstrip())
    return _section("\n".join(lines))


def _signal_block(signal: Signal, label_of: Dict[str, str]) -> Dict[str, Any]:
    evidence = " · ".join(_link(url, label_of.get(url, "source")) for url in signal.evidence_urls)
    return _section(
        f"*Signal:* {_escape(signal.signal)} _({SIGNAL_LEVELS[signal.evidence_level]})_\n"
        f"*Evidence:* {evidence}\n"
        f"*Interpretation:* {_escape(signal.interpretation)}\n"
        f"*What to watch next:* {_escape(signal.what_to_watch)}"
    )


def build_payload(report: DailyReport, tz_name: str = "Europe/Paris") -> Dict[str, Any]:
    tz = ZoneInfo(tz_name)
    today = datetime.now(timezone.utc).astimezone(tz).strftime("%A %d %B %Y")
    counts = "   ".join(
        f"{badge} {sum(e.priority == level for e in report.events)}"
        for level, badge in BADGES.items()
    )

    blocks: List[Dict[str, Any]] = [
        {"type": "header", "text": {"type": "plain_text", "text": f"☕ Tech Watch — {today}"}},
        _section(f"*Today's intelligence*\n{_escape(report.intro)}"),
        _context(counts),
    ]
    if not report.events:
        blocks.append(_section("_Nothing actionable today._"))

    # Categories appear in the order of their best-ranked event.
    groups: Dict[str, List[IntelligenceEvent]] = {}
    for event in report.events:
        groups.setdefault(event.category, []).append(event)

    budget = MAX_BLOCKS - (2 + len(report.signals) if report.signals else 0) - 4  # "…and more", legend, AI notice, footer
    shown = 0
    for category, events in groups.items():
        if len(blocks) + 3 > budget:
            break
        blocks += [{"type": "divider"}, _section(f"*{CATEGORIES[category]}*")]
        for event in events:
            if len(blocks) >= budget:
                break
            blocks.append(_event_block(event, tz))
            shown += 1
    if shown < len(report.events):
        blocks.append(_context(f"…and {len(report.events) - shown} more"))

    if report.signals:
        label_of = {s.url: s.name for e in report.events for s in e.sources}
        label_of = {**report.source_names, **label_of}
        blocks += [{"type": "divider"}, _section("*📡 Signals*")]
        blocks += [_signal_block(s, label_of) for s in report.signals]

    blocks.append(_context(EVIDENCE_LEGEND))
    blocks.append(_context(AI_NOTICE))
    blocks.append(_context(f"Model {report.model or 'n/a'} · prompt {report.prompt_version}"))
    return {
        "text": f"Tech Watch: {_escape(report.intro)}",
        "blocks": blocks,
        "unfurl_links": False,  # no automatic link preview under the report
        "unfurl_media": False,
    }


def post_payload(payload: Dict[str, Any], webhook_url: str, dry_run: bool) -> bool:
    """Post a Block Kit payload, or print it on a dry run. Returns True when posted."""
    if dry_run:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        logger.info("DRY_RUN enabled: Slack payload printed, not posted")
        return False
    try:
        resp = requests.post(webhook_url, json=payload, timeout=15)
    except requests.RequestException as exc:
        # The webhook URL is a secret and requests puts it in its error messages: the logs
        # (public for a public repository) only get the error type.
        raise RuntimeError(f"Slack webhook request failed: {type(exc).__name__}") from None
    if resp.status_code != 200:
        raise RuntimeError(f"Slack webhook returned {resp.status_code}: {resp.text[:200]}")
    return True


def send_to_slack(
    report: DailyReport, webhook_url: str, dry_run: bool = False, tz_name: str = "Europe/Paris"
) -> None:
    if post_payload(build_payload(report, tz_name), webhook_url, dry_run):
        logger.info("Report posted to Slack (%d event(s))", len(report.events))
