"""Tests for the Weekly Radar: window, validation of model output, Slack rendering (no network)."""

from __future__ import annotations

import sys
import types
import unittest
from datetime import datetime, timezone

try:  # the analyzer imports the SDK at module level; tests never call the API
    import anthropic  # noqa: F401
except ImportError:
    sys.modules["anthropic"] = types.ModuleType("anthropic")

from src.weekly import (
    Reading, Theme, TopStory, WeeklyOutput, build_weekly_payload, postprocess_weekly,
    week_label, week_window,
)


def stored_event(event_id, title, url, day, priority="INFO", evidence="CONFIRMED",
                 category="AI_ENGINEERING", source="OpenAI News", type_="OFFICIAL"):
    """An event as read back from Supabase (events.payload + posted_at)."""
    return {
        "event_id": event_id, "title": title, "category": category, "priority": priority,
        "evidence_status": evidence, "summary": "s", "facts": ["f"], "why_it_matters": "w",
        "sources": [{"name": source, "url": url, "type": type_, "role": "PRIMARY",
                     "published_at": f"{day}T08:00:00+00:00"}],
        "posted_at": f"{day}T07:10:00+00:00",
    }


EVENTS = [
    stored_event("evt_20261116_001", "Nvidia CEO transition",
                 "https://nvidianews.nvidia.com/news/nvidia-announces-ceo-transition",
                 "2026-11-16", priority="MAJOR", category="TECH_STARTUPS",
                 source="NVIDIA Newsroom"),
    stored_event("evt_20261117_002", "ARTEX tool used in attacks", "https://www.bleepingcomputer.com/artex",
                 "2026-11-17", priority="CRITICAL", evidence="REPORTED", category="CYBERSECURITY",
                 source="BleepingComputer", type_="MEDIA"),
    stored_event("evt_20261118_001", "LangChain core 1.8.1", "https://github.com/langchain-ai/langchain/releases/x",
                 "2026-11-18"),
]
READING = [{
    "source": "Harvard Business Review", "source_type": "MEDIA",
    "title": "How Nvidia Can Move Forward Without Its Founder",
    "url": "https://hbr.org/2026/11/how-nvidia-can-move-forward", "published_at":
    "2026-11-13T21:40:00+00:00", "content": "c",
}]


def output(**overrides):
    base = dict(
        intro="A week of leadership change and security alerts.",
        top_stories=[TopStory(event_id="evt_20261116_001", takeaway="t1"),
                     TopStory(event_id="evt_20261117_002", takeaway="t2")],
        themes=[],
        worth_reading=[],
    )
    base.update(overrides)
    return WeeklyOutput(**base)


class WindowTests(unittest.TestCase):
    def test_scheduled_run_covers_previous_calendar_week_in_paris_time(self):
        monday = datetime(2026, 11, 23, 7, 40, tzinfo=timezone.utc)  # 08:40 in Paris
        start, end = week_window(monday, "Europe/Paris", calendar_week=True)
        self.assertEqual(start.isoformat(), "2026-11-16T00:00:00+01:00")
        self.assertEqual(end.isoformat(), "2026-11-23T00:00:00+01:00")
        self.assertEqual(week_label(start, end), "Week 47 · 16 Nov – 22 Nov 2026")

    def test_manual_run_covers_last_seven_days(self):
        now = datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc)
        start, end = week_window(now, "Europe/Paris", calendar_week=False)
        self.assertEqual((end - start).days, 7)
        self.assertEqual(end, now)
        self.assertTrue(week_label(start, end, calendar_week=False).startswith("Last 7 days"))


class ValidationTests(unittest.TestCase):
    def test_unknown_or_repeated_top_stories_are_dropped(self):
        report = postprocess_weekly(output(top_stories=[
            TopStory(event_id="evt_20261116_001", takeaway="a"),
            TopStory(event_id="evt_invented", takeaway="b"),
            TopStory(event_id="evt_20261116_001", takeaway="c"),
        ]), EVENTS, READING, "Week 47")
        self.assertEqual([s.event_id for s in report.top_stories], ["evt_20261116_001"])
        # Title, link and source come from storage, not from the model.
        self.assertEqual(report.top_stories[0].url,
                         "https://nvidianews.nvidia.com/news/nvidia-announces-ceo-transition")
        self.assertEqual(report.top_stories[0].source_name, "NVIDIA Newsroom")

    def test_theme_needs_two_known_events(self):
        report = postprocess_weekly(output(themes=[
            Theme(title="Thin", synthesis="s", what_to_watch="w",
                  event_ids=["evt_20261116_001", "evt_invented"]),
            Theme(title="Solid", synthesis="s", what_to_watch="w",
                  event_ids=["evt_20261116_001", "evt_20261117_002"]),
        ]), EVENTS, READING, "Week 47")
        self.assertEqual([t.title for t in report.themes], ["Solid"])

    def test_reading_links_must_come_from_reading_feeds(self):
        report = postprocess_weekly(output(worth_reading=[
            Reading(url="https://hbr.org/2026/11/how-nvidia-can-move-forward", why="w"),
            Reading(url="https://example.com/not-in-feed", why="w"),
        ]), EVENTS, READING, "Week 47")
        self.assertEqual([r.title for r in report.reading],
                         ["How Nvidia Can Move Forward Without Its Founder"])

    def test_stats_are_computed_in_code(self):
        report = postprocess_weekly(output(), EVENTS, READING, "Week 47")
        self.assertEqual(report.stats["events"], 3)
        self.assertEqual(report.stats["digests"], 3)
        self.assertEqual(report.stats["confirmed"], 2)
        self.assertEqual(report.stats["priority"], {"MAJOR": 1, "CRITICAL": 1, "INFO": 1})


class WeeklySlackTests(unittest.TestCase):
    def test_payload_sections(self):
        report = postprocess_weekly(output(
            themes=[Theme(title="Leadership & risk", synthesis="s", what_to_watch="w",
                          event_ids=["evt_20261116_001", "evt_20261117_002"])],
            worth_reading=[Reading(url="https://hbr.org/2026/11/how-nvidia-can-move-forward",
                                   why="w")],
        ), EVENTS, READING, "Week 47 · 16 Nov – 22 Nov 2026")
        payload = build_weekly_payload(report)
        text = "\n".join(b.get("text", {}).get("text", "") if b["type"] != "context"
                         else b["elements"][0]["text"] for b in payload["blocks"])
        self.assertIn("Weekly Radar — Week 47", text)
        self.assertIn("*🏆 Top stories*", text)
        self.assertIn("*🧵 Themes of the week*", text)
        self.assertIn("*📚 Worth reading*", text)
        self.assertIn("3 events in 3 digests · 2 ✅ officially confirmed", text)
        self.assertIn("📰 press report, not officially confirmed", text)
        self.assertIn("📰 press report: covered by the media only", text)  # legend
        self.assertIn("AI-generated and may contain errors", text)
        self.assertLessEqual(len(payload["blocks"]), 50)
        self.assertFalse(payload["unfurl_links"])


if __name__ == "__main__":
    unittest.main()
