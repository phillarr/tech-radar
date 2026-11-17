"""Non-regression tests built from the 14 November 2026 Slack digests (spec annex).

They exercise the deterministic layer (postprocess + Slack rendering) with model outputs
that reproduce the mistakes seen in production. Run with:  python -m unittest discover tests
"""

from __future__ import annotations

import sys
import types
import unittest
from datetime import datetime, timezone

try:  # the analyzer imports the SDK at module level; tests never call the API
    import anthropic  # noqa: F401
except ImportError:
    sys.modules["anthropic"] = types.ModuleType("anthropic")

from src.analyzer import (
    AnalysisOutput,
    Event,
    EventSource,
    RecommendedAction,
    Scores,
    Signal,
    postprocess,
)
from src.notifier import build_payload

NOW = datetime(2026, 11, 14, 7, 0, tzinfo=timezone.utc)


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


class DeduplicationTests(unittest.TestCase):
    def test_same_url_in_two_events_is_published_once(self):
        # 14 Nov: "Ggml v1.0.0" appeared twice, the second time with a placeholder summary.
        ggml = "https://github.com/ggml-org/ggml/releases/tag/v1.0.0"
        report = run([
            event("Ggml 1.0", [(ggml, "PRIMARY")], priority="MAJOR"),
            event("Ggml 1.0 again", [(ggml, "PRIMARY")], summary="placeholder"),
        ])
        self.assertEqual(len(report.events), 1)
        self.assertEqual(report.events[0].title, "Ggml 1.0")

    def test_coverage_of_one_announcement_is_one_event_with_official_primary(self):
        # 14 Nov: ChatGPT Wallet was attributed to a media article although the
        # official OpenAI post was in the input.
        report = run([
            event("ChatGPT Wallet", [
                ("https://www.theverge.com/openai-chatgpt-wallet", "PRIMARY"),
                ("https://openai.com/index/introducing-chatgpt-wallet", "SUPPORTING"),
                ("https://techcrunch.com/chatgpt-wallet", "SUPPORTING"),
            ]),
        ])
        ev = report.events[0]
        self.assertEqual(ev.sources[0].name, "OpenAI News")
        self.assertEqual(ev.sources[0].role, "PRIMARY")
        self.assertEqual([s.role for s in ev.sources[1:]], ["SUPPORTING", "SUPPORTING"])
        self.assertEqual(ev.source_type, "OFFICIAL")


class TraceabilityTests(unittest.TestCase):
    def test_unknown_urls_are_dropped_and_sourceless_events_removed(self):
        report = run([
            event("Invented", [("https://hallucinated.example/x", "PRIMARY")]),
            event("Real", [("https://techcrunch.com/langchain-3b", "PRIMARY")]),
        ])
        self.assertEqual([e.title for e in report.events], ["Real"])

    def test_source_name_and_type_come_from_config_not_model(self):
        report = run([event("LangChain", [("https://techcrunch.com/langchain-3b", "PRIMARY")])])
        src = report.events[0].sources[0]
        self.assertEqual((src.name, src.type), ("TechCrunch AI", "MEDIA"))

    def test_confirmed_without_official_source_becomes_reported(self):
        report = run([event("LangChain", [("https://techcrunch.com/langchain-3b", "PRIMARY")],
                            evidence_status="CONFIRMED")])
        self.assertEqual(report.events[0].evidence_status, "REPORTED")

    def test_unverified_events_are_dropped(self):
        report = run([
            event("Rumor", [("https://techcrunch.com/langchain-3b", "PRIMARY")],
                  evidence_status="UNVERIFIED"),
            event("ChatGPT Wallet",
                  [("https://openai.com/index/introducing-chatgpt-wallet", "PRIMARY")]),
        ])
        self.assertEqual([e.title for e in report.events], ["ChatGPT Wallet"])


class PriorityTests(unittest.TestCase):
    def test_critical_with_low_urgency_is_demoted(self):
        # 14 Nov: resolved incidents were over-ranked.
        report = run([
            event("Copilot incident (resolved)",
                  [("https://www.githubstatus.com/incidents/51drrqvsv66g", "PRIMARY")],
                  category="CLOUD_INFRASTRUCTURE", event_type="SERVICE_INCIDENT",
                  priority="CRITICAL", scores=Scores(relevance=70, impact=60, urgency=20)),
        ])
        self.assertEqual(report.events[0].priority, "MAJOR")

    def test_critical_requires_an_official_confirmation(self):
        # 17 Nov: a ScreenConnect campaign was CRITICAL on a single media report.
        report = run([
            event("Press-only attack", [("https://techcrunch.com/langchain-3b", "PRIMARY")],
                  category="CYBERSECURITY", priority="CRITICAL",
                  scores=Scores(relevance=90, impact=90, urgency=95)),
            event("Official incident",
                  [("https://www.githubstatus.com/incidents/51drrqvsv66g", "PRIMARY")],
                  category="CLOUD_INFRASTRUCTURE", priority="CRITICAL",
                  scores=Scores(relevance=90, impact=90, urgency=95)),
        ])
        self.assertEqual({e.title: e.priority for e in report.events},
                         {"Press-only attack": "MAJOR", "Official incident": "CRITICAL"})

    def test_ordering_priority_then_urgency_then_relevance_and_cap(self):
        report = run([
            event("info", [("https://techcrunch.com/langchain-3b", "PRIMARY")],
                  scores=Scores(relevance=99, impact=50, urgency=99)),
            event("major low", [("https://www.wired.com/story/microsoft-365-outage", "PRIMARY")],
                  priority="MAJOR", scores=Scores(relevance=50, impact=50, urgency=10)),
            event("major high", [("https://status.claude.com/incidents/sqn3fg1l96dl", "PRIMARY")],
                  priority="MAJOR", scores=Scores(relevance=50, impact=50, urgency=40)),
        ], max_events=2)
        self.assertEqual([e.title for e in report.events], ["major high", "major low"])
        self.assertEqual(report.events[0].event_id, "evt_20261114_001")

    def test_scores_are_clamped(self):
        report = run([event("x", [("https://techcrunch.com/langchain-3b", "PRIMARY")],
                            scores=Scores(relevance=140, impact=-5, urgency=50))])
        s = report.events[0].scores
        self.assertEqual((s.relevance, s.impact), (100, 0))


class SignalTests(unittest.TestCase):
    def test_signal_needs_two_known_items(self):
        report = run(
            [event("x", [("https://techcrunch.com/langchain-3b", "PRIMARY")])],
            signals=[
                Signal(signal="Cascading provider outages", evidence_level="WEAK_SIGNAL",
                       evidence_urls=["https://www.githubstatus.com/incidents/51drrqvsv66g",
                                      "https://status.claude.com/incidents/sqn3fg1l96dl",
                                      "https://bad.example"],
                       interpretation="i", what_to_watch="w"),
                Signal(signal="Thin", evidence_level="WEAK_SIGNAL",
                       evidence_urls=["https://techcrunch.com/langchain-3b", "https://bad.example"],
                       interpretation="i", what_to_watch="w"),
            ],
        )
        self.assertEqual([s.signal for s in report.signals], ["Cascading provider outages"])
        self.assertEqual(len(report.signals[0].evidence_urls), 2)


class SlackRenderingTests(unittest.TestCase):
    def setUp(self):
        self.report = run(
            [
                event("ChatGPT <Wallet> | agents", [
                    ("https://openai.com/index/introducing-chatgpt-wallet", "PRIMARY"),
                    ("https://www.theverge.com/openai-chatgpt-wallet", "SUPPORTING"),
                ], product_opportunity="Agentic checkout & consent UX.", priority="MAJOR"),
                event("Copilot incident", [
                    ("https://www.githubstatus.com/incidents/51drrqvsv66g", "PRIMARY")],
                    category="CLOUD_INFRASTRUCTURE"),
            ],
            signals=[Signal(signal="s", evidence_level="CORROBORATED",
                            evidence_urls=["https://www.githubstatus.com/incidents/51drrqvsv66g",
                                           "https://status.claude.com/incidents/sqn3fg1l96dl"],
                            interpretation="i", what_to_watch="w")],
        )
        self.payload = build_payload(self.report)
        self.text = "\n".join(
            b["text"]["text"] for b in self.payload["blocks"] if b["type"] == "section"
        )

    def test_structure(self):
        self.assertFalse(self.payload["unfurl_links"])
        self.assertLessEqual(len(self.payload["blocks"]), 50)
        for label in ("What happened:", "Why it matters:", "Recommended action:",
                      "Product opportunity:", "📡 Signals", "What to watch next:",
                      "🧭 AI Product Management", "☁️ Cloud & Infrastructure"):
            self.assertIn(label, self.text)

    def test_escaping_and_traceability(self):
        self.assertIn("ChatGPT &lt;Wallet&gt; ¦ agents", self.text)
        self.assertIn("Agentic checkout &amp; consent UX.", self.text)
        self.assertIn("Source: OpenAI News (official) · 2 sources · ✅ officially confirmed · 13 Nov 19:00",
                      self.text)  # 18:00 UTC shown in Europe/Paris
        self.assertIn("|Status · Claude>", self.text)  # signal evidence labelled by source


class SlackLinkTests(unittest.TestCase):
    def test_feed_urls_cannot_break_the_link_markup(self):
        from src.notifier import _link

        # A crafted feed link must not close the link and inject a channel-wide mention.
        link = _link("https://evil.example/a><!channel>|x", "Title")
        self.assertNotIn("<!channel>", link)
        self.assertEqual(link.count("<"), 1)
        self.assertEqual(link.count(">"), 1)
        self.assertTrue(link.endswith("|Title>"))

    def test_only_http_links_are_clickable(self):
        from src.notifier import _link

        self.assertEqual(_link("javascript:alert(1)", "Title"), "Title")
        self.assertEqual(_link("https://a.com/x?a=1&b=2", "T"), "<https://a.com/x?a=1&amp;b=2|T>")


class FetcherTests(unittest.TestCase):
    def test_relative_feed_links_are_resolved_against_the_site(self):
        from src.fetcher import _absolute

        self.assertEqual(_absolute("/2026/11/x", "http://hbr.org"), "http://hbr.org/2026/11/x")
        self.assertEqual(_absolute("https://a.com/b", "http://hbr.org"), "https://a.com/b")

    def test_github_release_source_is_the_repository_not_the_release_name(self):
        # 17 Nov: the vLLM release was shown as "Source: v0.34.0 (official)".
        from unittest import mock
        from src.fetcher import fetch_github_releases

        resp = mock.Mock(status_code=200)
        resp.json.return_value = {"name": "v0.34.0", "html_url": "https://github.com/x/r",
                                  "published_at": "2026-11-17T00:22:00Z", "body": "notes"}
        with mock.patch("src.fetcher.requests.get", return_value=resp):
            items, _ = fetch_github_releases(["vllm-project/vllm"],
                                             datetime(2026, 11, 16, tzinfo=timezone.utc), 500, 5)
        self.assertEqual(items[0]["source"], "GitHub · vllm-project/vllm")
        self.assertEqual(items[0]["title"], "vllm-project/vllm v0.34.0")


if __name__ == "__main__":
    unittest.main()
