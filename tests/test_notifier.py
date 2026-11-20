"""Slack rendering, link safety and webhook posting."""

from __future__ import annotations

import unittest

from src.analyzer import Signal
from src.notifier import build_payload
from tests.helpers import event, run


class SlackRenderingTests(unittest.TestCase):
    def setUp(self):
        self.report = run(
            [
                event(
                    "ChatGPT <Wallet> | agents",
                    [
                        ("https://openai.com/index/introducing-chatgpt-wallet", "PRIMARY"),
                        ("https://www.theverge.com/openai-chatgpt-wallet", "SUPPORTING"),
                    ],
                    product_opportunity="Agentic checkout & consent UX.",
                    priority="MAJOR",
                ),
                event(
                    "Copilot incident",
                    [("https://www.githubstatus.com/incidents/51drrqvsv66g", "PRIMARY")],
                    category="CLOUD_INFRASTRUCTURE",
                ),
            ],
            signals=[
                Signal(
                    signal="s",
                    evidence_level="CORROBORATED",
                    evidence_urls=[
                        "https://www.githubstatus.com/incidents/51drrqvsv66g",
                        "https://status.claude.com/incidents/sqn3fg1l96dl",
                    ],
                    interpretation="i",
                    what_to_watch="w",
                )
            ],
        )
        self.payload = build_payload(self.report)
        self.text = "\n".join(
            b["text"]["text"] for b in self.payload["blocks"] if b["type"] == "section"
        )

    def test_structure(self):
        self.assertFalse(self.payload["unfurl_links"])
        self.assertLessEqual(len(self.payload["blocks"]), 50)
        for label in (
            "What happened:",
            "Why it matters:",
            "Recommended action:",
            "Product opportunity:",
            "📡 Signals",
            "What to watch next:",
            "🧭 AI Product Management",
            "☁️ Cloud & Infrastructure",
        ):
            self.assertIn(label, self.text)

    def test_escaping_and_traceability(self):
        self.assertIn("ChatGPT &lt;Wallet&gt; ¦ agents", self.text)
        self.assertIn("Agentic checkout &amp; consent UX.", self.text)
        self.assertIn(
            "Source: OpenAI News (official) · 2 sources · ✅ officially confirmed · 13 Nov 19:00",
            self.text,
        )  # 18:00 UTC shown in Europe/Paris
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


class SlackPostingTests(unittest.TestCase):
    def test_webhook_url_never_appears_in_errors(self):
        from unittest import mock

        import requests

        from src.notifier import post_payload

        secret = "https://hooks.slack.com/services/T000/B000/SECRET"
        err = requests.ConnectionError(f"Max retries exceeded with url: {secret}")
        with (
            mock.patch("src.notifier.requests.post", side_effect=err),
            self.assertRaises(RuntimeError) as ctx,
        ):
            post_payload({"text": "x"}, secret, dry_run=False)
        self.assertNotIn("SECRET", str(ctx.exception))
        self.assertIsNone(ctx.exception.__cause__)
        self.assertTrue(ctx.exception.__suppress_context__)

    def test_dates_without_time_are_shown_without_time(self):
        from zoneinfo import ZoneInfo

        from src.notifier import _local_time

        self.assertEqual(_local_time("2026-11-16", ZoneInfo("Europe/Paris")), "16 Nov")


if __name__ == "__main__":
    unittest.main()
