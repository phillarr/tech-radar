"""Pipeline orchestrator: config -> fetch -> analyze -> notify.

Run from the project root with:  python -m src.main
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

if __package__ in (None, ""):  # allow `python src/main.py` as well
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.analyzer import DailyTechReport, analyze
from src.config_loader import load_all
from src.fetcher import fetch_all
from src.notifier import send_to_slack

logger = logging.getLogger("tech_watch")


def run() -> int:
    secrets, config = load_all()
    settings = config.settings

    items = fetch_all(config, github_token=secrets.github_token)
    if not items:
        logger.info("No items in the last %dh; skipping the LLM call.", settings.hours_lookback)
        if settings.post_when_empty:
            empty = DailyTechReport(
                headline="Quiet morning: no new updates from tracked sources.", alerts=[], signals=[]
            )
            send_to_slack(empty, secrets.slack_webhook_url, secrets.dry_run)
        return 0

    report = analyze(
        items,
        secrets.anthropic_api_key,
        settings.model_name,
        settings.effort,
        settings.audience,
        settings.max_alerts,
    )
    logger.info("Analysis kept %d of %d item(s)", len(report.alerts), len(items))

    if not report.alerts and not settings.post_when_empty:
        logger.info("No actionable alerts; nothing posted.")
        return 0

    send_to_slack(report, secrets.slack_webhook_url, secrets.dry_run)
    return 0


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        sys.exit(run())
    except Exception:
        logger.exception("Tech watch run failed")
        sys.exit(1)


if __name__ == "__main__":
    main()
