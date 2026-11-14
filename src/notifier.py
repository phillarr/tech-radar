"""Slack Block Kit message builder and Incoming Webhook sender."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List

import requests

from src.analyzer import DailyTechReport, TechAlert

logger = logging.getLogger(__name__)

BADGES = {
    "CRITICAL": "🔴 [CRITICAL]",
    "MAJOR": "🟡 [MAJOR]",
    "INFO": "🔵 [INFO]",
}
# Slack limits: 50 blocks per message, 3000 chars per section text.
MAX_BLOCKS = 50
MAX_SECTION_CHARS = 3000


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _alert_block(alert: TechAlert) -> Dict[str, Any]:
    title = _escape(alert.title).replace("|", "¦")
    text = (
        f"*{BADGES[alert.importance]}* <{alert.url}|{title}>\n"
        f"{_escape(alert.impact_summary)}\n"
        f"_{_escape(alert.source)}_"
    )
    return {"type": "section", "text": {"type": "mrkdwn", "text": text[:MAX_SECTION_CHARS]}}


def build_payload(report: DailyTechReport) -> Dict[str, Any]:
    today = datetime.now(timezone.utc).strftime("%A %d %B %Y")
    counts = {level: sum(a.importance == level for a in report.alerts) for level in BADGES}
    summary = "  ".join(f"{BADGES[level]} {n}" for level, n in counts.items())

    blocks: List[Dict[str, Any]] = [
        {"type": "header", "text": {"type": "plain_text", "text": f"☕ Tech Watch — {today}"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*{_escape(report.headline)}*"}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text": summary}]},
    ]

    if not report.alerts:
        blocks.append(
            {"type": "section", "text": {"type": "mrkdwn", "text": "_Nothing actionable today._"}}
        )

    current_level = None
    for alert in report.alerts:
        if len(blocks) >= MAX_BLOCKS - 2:
            remaining = len(report.alerts) - report.alerts.index(alert)
            blocks.append(
                {"type": "context", "elements": [{"type": "mrkdwn", "text": f"…and {remaining} more"}]}
            )
            break
        if alert.importance != current_level:
            blocks.append({"type": "divider"})
            current_level = alert.importance
        blocks.append(_alert_block(alert))

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
