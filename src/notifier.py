"""Slack Block Kit message builder and Incoming Webhook sender."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List

import requests

from src.analyzer import DailyTechReport, Signal, TechAlert

logger = logging.getLogger(__name__)

BADGES = {
    "CRITICAL": "🔴 [CRITICAL]",
    "MAJOR": "🟡 [MAJOR]",
    "INFO": "🔵 [INFO]",
}
AXIS_ICONS = {
    "AI & GenAI": "🤖",
    "AI Product Management": "🧭",
    "AI Engineering": "🛠️",
    "Cloud & Infrastructure": "☁️",
    "Tech & Startups": "🚀",
    "AI Governance": "⚖️",
}
SOURCE_TYPE_LABELS = {
    "OFFICIAL": "Official announcement",
    "EXPERIMENT": "Experiment / research",
    "BENCHMARK": "Independent benchmark",
    "NEWS": "News report",
    "OPINION": "Opinion",
}
# Slack limits: 50 blocks per message, 3000 chars per section text.
MAX_BLOCKS = 50
MAX_SECTION_CHARS = 3000


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _section(text: str) -> Dict[str, Any]:
    return {"type": "section", "text": {"type": "mrkdwn", "text": text[:MAX_SECTION_CHARS]}}


def _context(text: str) -> Dict[str, Any]:
    return {"type": "context", "elements": [{"type": "mrkdwn", "text": text}]}


def _alert_block(alert: TechAlert) -> Dict[str, Any]:
    title = _escape(alert.title).replace("|", "¦")
    return _section(
        f"*{BADGES[alert.importance]}* <{alert.url}|{title}>\n"
        f"{_escape(alert.impact_summary)}\n"
        f"_{_escape(alert.source)} · {SOURCE_TYPE_LABELS[alert.source_type]}_"
    )


def _signal_block(signal: Signal) -> Dict[str, Any]:
    links = " ".join(f"<{url}|[{i}]>" for i, url in enumerate(signal.related_urls, start=1))
    return _section(
        f"• *{_escape(signal.trend)}*\n"
        f"   ↳ _Opportunity:_ {_escape(signal.product_opportunity)}  {links}"
    )


def _group_by_axis(alerts: List[TechAlert]) -> List[List[TechAlert]]:
    """Group alerts by axis; groups and their items keep the score ranking."""
    groups: Dict[str, List[TechAlert]] = {}
    for alert in alerts:  # already sorted by score, so dict order = best group first
        groups.setdefault(alert.axis, []).append(alert)
    return list(groups.values())


def build_payload(report: DailyTechReport) -> Dict[str, Any]:
    today = datetime.now(timezone.utc).strftime("%A %d %B %Y")
    counts = {level: sum(a.importance == level for a in report.alerts) for level in BADGES}
    summary = "  ".join(f"{BADGES[level]} {n}" for level, n in counts.items())

    blocks: List[Dict[str, Any]] = [
        {"type": "header", "text": {"type": "plain_text", "text": f"☕ Tech Watch — {today}"}},
        _section(f"*{_escape(report.headline)}*"),
        _context(summary),
    ]
    if not report.alerts:
        blocks.append(_section("_Nothing actionable today._"))

    # Reserve room for the signals section and an overflow note.
    budget = MAX_BLOCKS - (2 + len(report.signals) if report.signals else 0) - 1
    shown = 0
    for group in _group_by_axis(report.alerts):
        if len(blocks) + 3 > budget:
            break
        axis = group[0].axis
        blocks += [{"type": "divider"}, _section(f"*{AXIS_ICONS[axis]} {axis}*")]
        for alert in group:
            if len(blocks) >= budget:
                break
            blocks.append(_alert_block(alert))
            shown += 1
    if shown < len(report.alerts):
        blocks.append(_context(f"…and {len(report.alerts) - shown} more"))

    if report.signals:
        blocks += [{"type": "divider"}, _section("*📡 Signals*")]
        blocks += [_signal_block(s) for s in report.signals]

    return {"text": f"Tech Watch: {_escape(report.headline)}", "blocks": blocks}


def send_to_slack(report: DailyTechReport, webhook_url: str, dry_run: bool = False) -> None:
    payload = build_payload(report)
    if dry_run:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        logger.info("DRY_RUN enabled: Slack payload printed, not posted")
        return

    resp = requests.post(webhook_url, json=payload, timeout=15)
    if resp.status_code != 200:
        raise RuntimeError(f"Slack webhook returned {resp.status_code}: {resp.text[:200]}")
    logger.info("Report posted to Slack (%d alert(s))", len(report.alerts))
