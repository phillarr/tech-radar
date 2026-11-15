"""Supabase persistence (PostgREST over HTTPS) and cross-day deduplication.

Storage is best effort: if Supabase is not configured or unreachable, the run continues
without history and the digest is still sent. Every failure is logged as a warning.
"""

from __future__ import annotations

import functools
import hashlib
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests

from src.analyzer import DailyReport
from src.fetcher import RawItem, SourceHealth

logger = logging.getLogger(__name__)

REPUBLISH_WINDOW_DAYS = 14  # an unchanged article published within this window is not re-sent
INACTIVE_AFTER_FAILURES = 3  # consecutive failed runs before a source is marked INACTIVE


class StorageError(RuntimeError):
    pass


# --- Pure helpers (unit tested) -------------------------------------------------------------


def content_hash(item: RawItem) -> str:
    text = f"{item['title'].strip()}\n{item['content'].strip()}"
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def filter_already_published(
    items: List[RawItem], published: Dict[str, str]
) -> Tuple[List[RawItem], int, int]:
    """Drop items already published unchanged; flag changed ones as updates.

    `published` maps url -> content hash at publication time.
    Returns (kept items, number dropped, number flagged as update).
    """
    kept: List[RawItem] = []
    dropped = updates = 0
    for item in items:
        previous = published.get(item["url"])
        if previous is None:
            kept.append(item)
        elif previous == content_hash(item):
            dropped += 1
        else:
            kept.append({**item, "update": True})
            updates += 1
    return kept, dropped, updates


def next_source_status(ok: bool, previous_failures: int) -> Tuple[str, int]:
    """Return (status, consecutive_failures) after one fetch attempt."""
    if ok:
        return "ACTIVE", 0
    failures = previous_failures + 1
    return ("INACTIVE" if failures >= INACTIVE_AFTER_FAILURES else "DEGRADED"), failures


# --- Supabase client ------------------------------------------------------------------------


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _best_effort(fn):
    @functools.wraps(fn)
    def wrapper(self: "Storage", *args, **kwargs):
        try:
            return fn(self, *args, **kwargs)
        except (StorageError, requests.RequestException, ValueError) as exc:
            logger.warning("Supabase %s failed: %s", fn.__name__, exc)
            return None

    return wrapper


class Storage:
    def __init__(self, url: str, key: str, timeout: float = 15):
        self.base = url.rstrip("/") + "/rest/v1"
        self.timeout = timeout
        self.session = requests.Session()
        headers = {"apikey": key, "Content-Type": "application/json"}
        if not key.startswith("sb_"):  # legacy service_role JWT
            headers["Authorization"] = f"Bearer {key}"
        self.session.headers.update(headers)

    @classmethod
    def connect(cls, url: Optional[str], key: Optional[str]) -> Optional["Storage"]:
        """Return a working client, or None (with a clear log line) if storage is unusable."""
        if not url and not key:
            logger.info("Supabase not configured: running without history")
            return None
        if not url or not key:
            missing = "SUPABASE_URL" if not url else "SUPABASE_SERVICE_KEY"
            logger.warning("Supabase disabled: %s is empty", missing)
            return None
        store = cls(url, key)
        try:
            store._request("GET", "sources", params={"select": "name", "limit": "1"})
        except (StorageError, requests.RequestException) as exc:
            logger.warning("Supabase disabled: %s", exc)
            return None
        logger.info("Supabase connected")
        return store

    def _request(
        self,
        method: str,
        table: str,
        params: Optional[Dict[str, str]] = None,
        body: Any = None,
        prefer: Optional[str] = None,
    ) -> Any:
        resp = self.session.request(
            method,
            f"{self.base}/{table}",
            params=params,
            json=body,
            headers={"Prefer": prefer} if prefer else None,
            timeout=self.timeout,
        )
        if resp.status_code in (401, 403):
            raise StorageError(f"key rejected (HTTP {resp.status_code}): check SUPABASE_SERVICE_KEY")
        if resp.status_code == 404 and "PGRST205" in resp.text:
            raise StorageError(f"table '{table}' not found: run db/schema.sql in Supabase")
        if resp.status_code >= 300:
            raise StorageError(f"{method} {table}: HTTP {resp.status_code} {resp.text[:300]}")
        return resp.json() if resp.content else None

    def _upsert(self, table: str, rows: List[Dict[str, Any]], on_conflict: str) -> None:
        if rows:
            self._request(
                "POST",
                table,
                params={"on_conflict": on_conflict},
                body=rows,
                prefer="resolution=merge-duplicates,return=minimal",
            )

    def _insert(self, table: str, rows: List[Dict[str, Any]], returning: bool = False) -> Any:
        if not rows:
            return []
        return self._request(
            "POST", table, body=rows,
            prefer="return=representation" if returning else "return=minimal",
        )

    # --- Ingestion ---------------------------------------------------------------------------

    @_best_effort
    def record_sources(self, health: List[SourceHealth]) -> None:
        rows = self._request(
            "GET", "sources", params={"select": "name,consecutive_failures,last_item_at"}
        ) or []
        previous = {r["name"]: r for r in rows}
        now = _now()
        ok_rows, failed_rows = [], []
        for h in health:
            prev = previous.get(h["name"], {})
            status, failures = next_source_status(h["ok"], prev.get("consecutive_failures") or 0)
            row = {
                "name": h["name"], "url": h["url"], "kind": h["kind"], "type": h["type"],
                "status": status, "consecutive_failures": failures, "last_checked_at": now,
            }
            if h["ok"]:
                cursors = [c for c in (prev.get("last_item_at"), h["last_item_at"]) if c]
                latest = max(cursors, key=lambda c: datetime.fromisoformat(c)) if cursors else None
                ok_rows.append({**row, "last_success_at": now, "last_item_at": latest})
            else:
                failed_rows.append({**row, "last_error": (h["error"] or "")[:500]})
        self._upsert("sources", ok_rows, "name")
        self._upsert("sources", failed_rows, "name")
        degraded = [r["name"] for r in failed_rows]
        if degraded:
            logger.info("Sources not ACTIVE this run: %s", ", ".join(degraded))

    @_best_effort
    def record_articles(self, items: List[RawItem]) -> None:
        now = _now()
        self._upsert("articles", [self._article_row(i, now) for i in items], "url")

    @_best_effort
    def published_hashes(self) -> Dict[str, str]:
        since = (datetime.now(timezone.utc) - timedelta(days=REPUBLISH_WINDOW_DAYS)).isoformat()
        rows = self._request(
            "GET", "articles",
            params={"select": "url,last_published_hash", "last_published_at": f"gte.{since}"},
        ) or []
        return {r["url"]: r["last_published_hash"] for r in rows if r["last_published_hash"]}

    @staticmethod
    def _article_row(item: RawItem, now: str) -> Dict[str, Any]:
        return {
            "url": item["url"],
            "source_name": item["source"],
            "source_type": item["source_type"],
            "title": item["title"],
            "published_at": item["published_at"] or None,
            "content": item["content"],
            "content_hash": content_hash(item),
            "last_seen_at": now,
        }

    # --- Analysis and distribution -----------------------------------------------------------

    @_best_effort
    def posted_since(self, since: datetime) -> bool:
        rows = self._request("GET", "distributions", params={
            "select": "id", "status": "eq.POSTED",
            "created_at": f"gte.{since.astimezone(timezone.utc).isoformat()}", "limit": "1",
        }) or []
        return bool(rows)

    @_best_effort
    def record_analysis(self, report: DailyReport, items_in: int, dry_run: bool) -> Optional[str]:
        rows = self._insert("analyses", [{
            "model": report.model,
            "prompt_version": report.prompt_version,
            "stop_reason": report.stop_reason,
            "input_tokens": report.input_tokens,
            "output_tokens": report.output_tokens,
            "items_in": items_in,
            "events_out": len(report.events),
            "signals_out": len(report.signals),
            "dry_run": dry_run,
        }], returning=True)
        analysis_id = rows[0]["id"]

        self._insert("events", [{
            "analysis_id": analysis_id,
            "event_id": e.event_id,
            "title": e.title,
            "category": e.category,
            "secondary_categories": e.secondary_categories,
            "event_type": e.event_type,
            "priority": e.priority,
            "evidence_status": e.evidence_status,
            "source_type": e.source_type,
            "primary_url": e.sources[0].url,
            "relevance": e.scores.relevance,
            "impact": e.scores.impact,
            "urgency": e.scores.urgency,
            "payload": e.model_dump(mode="json"),
        } for e in report.events])

        self._insert("signals", [{
            "analysis_id": analysis_id,
            "signal": s.signal,
            "evidence_level": s.evidence_level,
            "evidence_urls": s.evidence_urls,
            "interpretation": s.interpretation,
            "what_to_watch": s.what_to_watch,
        } for s in report.signals])
        return analysis_id

    @_best_effort
    def record_outcome(
        self, items: List[RawItem], report: DailyReport, analysis_id: Optional[str], posted: bool
    ) -> None:
        """Update article statuses and log the distribution."""
        now = _now()
        event_of = {s.url: e.event_id for e in report.events for s in e.sources}

        selected = [i for i in items if i["url"] in event_of]
        rejected = [i for i in items if i["url"] not in event_of]
        self._upsert("articles", [
            {**self._article_row(i, now), "status": "REJECTED"} for i in rejected
        ], "url")
        if posted:
            self._upsert("articles", [{
                **self._article_row(i, now),
                "status": "PUBLISHED",
                "last_published_hash": content_hash(i),
                "last_published_at": now,
                "last_event_id": event_of[i["url"]],
            } for i in selected], "url")
        else:
            self._upsert("articles", [
                {**self._article_row(i, now), "status": "ANALYZED"} for i in selected
            ], "url")

        if analysis_id:
            self._insert("distributions", [{
                "analysis_id": analysis_id,
                "channel": "slack",
                "status": "POSTED" if posted else "DRY_RUN",
                "event_ids": [e.event_id for e in report.events],
            }])
