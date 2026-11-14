"""Pipeline orchestrator: config -> fetch -> store -> dedupe -> analyze -> notify -> store.

Run from the project root with:  python -m src.main
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

if __package__ in (None, ""):  # allow `python src/main.py` as well
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.analyzer import DailyReport, analyze
from src.config_loader import load_all
from src.fetcher import fetch_all
from src.notifier import send_to_slack
from src.storage import Storage, filter_already_published

logger = logging.getLogger("tech_watch")


def run() -> int:
    secrets, config = load_all()
    settings = config.settings

    items, health = fetch_all(config, github_token=secrets.github_token)

    store = Storage.connect(secrets.supabase_url, secrets.supabase_service_key)
    if store:
        store.record_sources(health)
        store.record_articles(items)
        published = store.published_hashes()
        if published is not None:
            items, dropped, updates = filter_already_published(items, published)
            logger.info(
                "History: %d item(s) already published and unchanged, %d update(s) of "
                "published items", dropped, updates,
            )

    if not items:
        logger.info("No new items; skipping the LLM call.")
        if settings.post_when_empty:
            empty = DailyReport(
                intro="Quiet morning: no new updates from tracked sources.", events=[], signals=[]
            )
            send_to_slack(empty, secrets.slack_webhook_url, secrets.dry_run, settings.timezone)
        return 0

    report = analyze(
        items,
        secrets.anthropic_api_key,
        settings.model_name,
        settings.effort,
        settings.audience,
        settings.max_events,
    )
    covered = sum(len(e.sources) for e in report.events)
    logger.info(
        "Analysis kept %d event(s) covering %d of %d item(s), %d signal(s)",
        len(report.events), covered, len(items), len(report.signals),
    )
    analysis_id = store.record_analysis(report, len(items), secrets.dry_run) if store else None

    if not report.events and not settings.post_when_empty:
        logger.info("No actionable events; nothing posted.")
        if store:
            store.record_outcome(items, report, None, posted=False)  # nothing distributed
        return 0

    send_to_slack(report, secrets.slack_webhook_url, secrets.dry_run, settings.timezone)
    if store:
        store.record_outcome(items, report, analysis_id, posted=not secrets.dry_run)
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
