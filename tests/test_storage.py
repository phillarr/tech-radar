"""Tests for cross-day deduplication, source health and Supabase row building (no network)."""

from __future__ import annotations

import sys
import types
import unittest

try:  # the analyzer imports the SDK at module level; tests never call the API
    import anthropic  # noqa: F401
except ImportError:
    sys.modules["anthropic"] = types.ModuleType("anthropic")

from src.storage import (
    Storage,
    content_hash,
    filter_already_published,
    next_source_status,
)
from tests.test_pipeline import ITEMS, event, run


def item(url, content="body", title="Title"):
    return {
        "source": "Status · GitHub",
        "source_type": "OFFICIAL",
        "title": title,
        "url": url,
        "published_at": "2026-11-13T18:00:00+00:00",
        "content": content,
    }


class RepublicationTests(unittest.TestCase):
    def test_unchanged_published_item_is_dropped(self):
        old = item("https://a", "Investigating")
        kept, dropped, updates = filter_already_published([old], {"https://a": content_hash(old)})
        self.assertEqual((kept, dropped, updates), ([], 1, 0))

    def test_changed_published_item_is_kept_as_update(self):
        # A status-page incident keeps its URL while its content moves to "Resolved".
        before = item("https://a", "Investigating")
        after = item("https://a", "Investigating. Resolved at 10:00 UTC.")
        kept, dropped, updates = filter_already_published([after], {"https://a": content_hash(before)})
        self.assertEqual((dropped, updates), (0, 1))
        self.assertTrue(kept[0]["update"])

    def test_new_item_is_kept_untouched(self):
        new = item("https://b")
        kept, dropped, updates = filter_already_published([new], {})
        self.assertEqual(kept, [new])
        self.assertNotIn("update", kept[0])

    def test_hash_ignores_surrounding_whitespace(self):
        self.assertEqual(content_hash(item("u", " body ")), content_hash(item("u", "body")))


class SourceStatusTests(unittest.TestCase):
    def test_status_transitions(self):
        self.assertEqual(next_source_status(True, 5), ("ACTIVE", 0))
        self.assertEqual(next_source_status(False, 0), ("DEGRADED", 1))
        self.assertEqual(next_source_status(False, 1), ("DEGRADED", 2))
        self.assertEqual(next_source_status(False, 2), ("INACTIVE", 3))


class FakeStorage(Storage):
    """Records the requests instead of sending them."""

    def __init__(self):
        super().__init__("https://example.supabase.co", "sb_secret_test")
        self.calls = []

    def _request(self, method, table, params=None, body=None, prefer=None):
        self.calls.append((method, table, body))
        if table == "analyses":
            return [{"id": "00000000-0000-0000-0000-000000000001"}]
        return []


class StorageRowTests(unittest.TestCase):
    def setUp(self):
        self.report = run([
            event("ChatGPT Wallet", [
                ("https://openai.com/index/introducing-chatgpt-wallet", "PRIMARY"),
                ("https://techcrunch.com/chatgpt-wallet", "SUPPORTING"),
            ]),
        ])

    def test_new_secret_keys_are_sent_as_apikey_only(self):
        store = Storage("https://x.supabase.co", "sb_secret_abc")
        self.assertEqual(store.session.headers["apikey"], "sb_secret_abc")
        self.assertNotIn("Authorization", store.session.headers)

    def test_connect_without_configuration_disables_storage(self):
        self.assertIsNone(Storage.connect(None, None))
        self.assertIsNone(Storage.connect("https://x.supabase.co", ""))

    def test_published_articles_get_their_hash_and_event(self):
        store = FakeStorage()
        store.record_outcome(ITEMS, self.report, "aid", posted=True)
        published = next(b for m, t, b in store.calls if t == "articles" and b
                         and b[0]["status"] == "PUBLISHED")
        self.assertEqual({r["url"] for r in published}, {
            "https://openai.com/index/introducing-chatgpt-wallet",
            "https://techcrunch.com/chatgpt-wallet",
        })
        self.assertTrue(all(r["last_event_id"] == "evt_20261114_001" for r in published))
        self.assertTrue(all(r["last_published_hash"] for r in published))
        dist = next(b for m, t, b in store.calls if t == "distributions")
        self.assertEqual(dist[0]["status"], "POSTED")

    def test_dry_run_never_marks_articles_published(self):
        store = FakeStorage()
        store.record_outcome(ITEMS, self.report, "aid", posted=False)
        statuses = {r["status"] for m, t, b in store.calls if t == "articles" for r in b}
        self.assertEqual(statuses, {"ANALYZED", "REJECTED"})
        self.assertFalse(any("last_published_hash" in r
                             for m, t, b in store.calls if t == "articles" for r in b))

    def test_analysis_events_and_signals_rows(self):
        store = FakeStorage()
        analysis_id = store.record_analysis(self.report, items_in=8, dry_run=True)
        self.assertEqual(analysis_id, "00000000-0000-0000-0000-000000000001")
        events = next(b for m, t, b in store.calls if t == "events")
        self.assertEqual(events[0]["primary_url"],
                         "https://openai.com/index/introducing-chatgpt-wallet")
        self.assertEqual(events[0]["payload"]["event_id"], "evt_20261114_001")


if __name__ == "__main__":
    unittest.main()
