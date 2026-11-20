"""Deterministic checks applied to the model output: deduplication, traceability,
evidence, priorities and signals."""

from __future__ import annotations

import unittest

from src.analyzer import Scores, Signal
from tests.helpers import event, run


class DeduplicationTests(unittest.TestCase):
    def test_same_url_in_two_events_is_published_once(self):
        # 14 Nov: "Ggml v1.0.0" appeared twice, the second time with a placeholder summary.
        ggml = "https://github.com/ggml-org/ggml/releases/tag/v1.0.0"
        report = run(
            [
                event("Ggml 1.0", [(ggml, "PRIMARY")], priority="MAJOR"),
                event("Ggml 1.0 again", [(ggml, "PRIMARY")], summary="placeholder"),
            ]
        )
        self.assertEqual(len(report.events), 1)
        self.assertEqual(report.events[0].title, "Ggml 1.0")

    def test_coverage_of_one_announcement_is_one_event_with_official_primary(self):
        # 14 Nov: ChatGPT Wallet was attributed to a media article although the
        # official OpenAI post was in the input.
        report = run(
            [
                event(
                    "ChatGPT Wallet",
                    [
                        ("https://www.theverge.com/openai-chatgpt-wallet", "PRIMARY"),
                        ("https://openai.com/index/introducing-chatgpt-wallet", "SUPPORTING"),
                        ("https://techcrunch.com/chatgpt-wallet", "SUPPORTING"),
                    ],
                ),
            ]
        )
        ev = report.events[0]
        self.assertEqual(ev.sources[0].name, "OpenAI News")
        self.assertEqual(ev.sources[0].role, "PRIMARY")
        self.assertEqual([s.role for s in ev.sources[1:]], ["SUPPORTING", "SUPPORTING"])
        self.assertEqual(ev.source_type, "OFFICIAL")


class TraceabilityTests(unittest.TestCase):
    def test_unknown_urls_are_dropped_and_sourceless_events_removed(self):
        report = run(
            [
                event("Invented", [("https://hallucinated.example/x", "PRIMARY")]),
                event("Real", [("https://techcrunch.com/langchain-3b", "PRIMARY")]),
            ]
        )
        self.assertEqual([e.title for e in report.events], ["Real"])

    def test_source_name_and_type_come_from_config_not_model(self):
        report = run([event("LangChain", [("https://techcrunch.com/langchain-3b", "PRIMARY")])])
        src = report.events[0].sources[0]
        self.assertEqual((src.name, src.type), ("TechCrunch AI", "MEDIA"))

    def test_confirmed_without_official_source_becomes_reported(self):
        report = run(
            [
                event(
                    "LangChain",
                    [("https://techcrunch.com/langchain-3b", "PRIMARY")],
                    evidence_status="CONFIRMED",
                )
            ]
        )
        self.assertEqual(report.events[0].evidence_status, "REPORTED")

    def test_unverified_events_are_dropped(self):
        report = run(
            [
                event(
                    "Rumor",
                    [("https://techcrunch.com/langchain-3b", "PRIMARY")],
                    evidence_status="UNVERIFIED",
                ),
                event(
                    "ChatGPT Wallet",
                    [("https://openai.com/index/introducing-chatgpt-wallet", "PRIMARY")],
                ),
            ]
        )
        self.assertEqual([e.title for e in report.events], ["ChatGPT Wallet"])


class PriorityTests(unittest.TestCase):
    def test_critical_with_low_urgency_is_demoted(self):
        # 14 Nov: resolved incidents were over-ranked.
        report = run(
            [
                event(
                    "Copilot incident (resolved)",
                    [("https://www.githubstatus.com/incidents/51drrqvsv66g", "PRIMARY")],
                    category="CLOUD_INFRASTRUCTURE",
                    event_type="SERVICE_INCIDENT",
                    priority="CRITICAL",
                    scores=Scores(relevance=70, impact=60, urgency=20),
                ),
            ]
        )
        self.assertEqual(report.events[0].priority, "MAJOR")

    def test_critical_requires_an_official_confirmation(self):
        # 17 Nov: a ScreenConnect campaign was CRITICAL on a single media report.
        report = run(
            [
                event(
                    "Press-only attack",
                    [("https://techcrunch.com/langchain-3b", "PRIMARY")],
                    category="CYBERSECURITY",
                    priority="CRITICAL",
                    scores=Scores(relevance=90, impact=90, urgency=95),
                ),
                event(
                    "Official incident",
                    [("https://www.githubstatus.com/incidents/51drrqvsv66g", "PRIMARY")],
                    category="CLOUD_INFRASTRUCTURE",
                    priority="CRITICAL",
                    scores=Scores(relevance=90, impact=90, urgency=95),
                ),
            ]
        )
        self.assertEqual(
            {e.title: e.priority for e in report.events},
            {"Press-only attack": "MAJOR", "Official incident": "CRITICAL"},
        )

    def test_ordering_priority_then_urgency_then_relevance_and_cap(self):
        report = run(
            [
                event(
                    "info",
                    [("https://techcrunch.com/langchain-3b", "PRIMARY")],
                    scores=Scores(relevance=99, impact=50, urgency=99),
                ),
                event(
                    "major low",
                    [("https://www.wired.com/story/microsoft-365-outage", "PRIMARY")],
                    priority="MAJOR",
                    scores=Scores(relevance=50, impact=50, urgency=10),
                ),
                event(
                    "major high",
                    [("https://status.claude.com/incidents/sqn3fg1l96dl", "PRIMARY")],
                    priority="MAJOR",
                    scores=Scores(relevance=50, impact=50, urgency=40),
                ),
            ],
            max_events=2,
        )
        self.assertEqual([e.title for e in report.events], ["major high", "major low"])
        self.assertEqual(report.events[0].event_id, "evt_20261114_001")

    def test_scores_are_clamped(self):
        report = run(
            [
                event(
                    "x",
                    [("https://techcrunch.com/langchain-3b", "PRIMARY")],
                    scores=Scores(relevance=140, impact=-5, urgency=50),
                )
            ]
        )
        s = report.events[0].scores
        self.assertEqual((s.relevance, s.impact), (100, 0))


class SignalTests(unittest.TestCase):
    def test_signal_needs_two_known_items(self):
        report = run(
            [event("x", [("https://techcrunch.com/langchain-3b", "PRIMARY")])],
            signals=[
                Signal(
                    signal="Cascading provider outages",
                    evidence_level="WEAK_SIGNAL",
                    evidence_urls=[
                        "https://www.githubstatus.com/incidents/51drrqvsv66g",
                        "https://status.claude.com/incidents/sqn3fg1l96dl",
                        "https://bad.example",
                    ],
                    interpretation="i",
                    what_to_watch="w",
                ),
                Signal(
                    signal="Thin",
                    evidence_level="WEAK_SIGNAL",
                    evidence_urls=["https://techcrunch.com/langchain-3b", "https://bad.example"],
                    interpretation="i",
                    what_to_watch="w",
                ),
            ],
        )
        self.assertEqual([s.signal for s in report.signals], ["Cascading provider outages"])
        self.assertEqual(len(report.signals[0].evidence_urls), 2)


if __name__ == "__main__":
    unittest.main()
