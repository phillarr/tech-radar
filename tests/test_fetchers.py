"""Source fetchers, with recorded HTTP responses (no network)."""

from __future__ import annotations

import unittest
from datetime import UTC, datetime

from tests.helpers import response


class FetcherTests(unittest.TestCase):
    def test_relative_feed_links_are_resolved_against_the_site(self):
        from src.fetcher.rss import absolute_url as _absolute

        self.assertEqual(_absolute("/2026/11/x", "http://hbr.org"), "http://hbr.org/2026/11/x")
        self.assertEqual(_absolute("https://a.com/b", "http://hbr.org"), "https://a.com/b")

    def test_github_release_source_is_the_repository_not_the_release_name(self):
        # 17 Nov: the vLLM release was shown as "Source: v0.34.0 (official)".
        from unittest import mock

        from src.fetcher import fetch_github_releases

        resp = response(
            json_body={
                "name": "v0.34.0",
                "html_url": "https://github.com/x/r",
                "published_at": "2026-11-17T00:22:00Z",
                "body": "notes",
            }
        )
        with mock.patch("src.fetcher.base.requests.get", return_value=resp):
            items, _ = fetch_github_releases(
                ["vllm-project/vllm"], datetime(2026, 11, 16, tzinfo=UTC), 500, 5
            )
        self.assertEqual(items[0]["source"], "GitHub · vllm-project/vllm")
        self.assertEqual(items[0]["title"], "vllm-project/vllm v0.34.0")

    def test_cisa_kev_keeps_entries_added_since_the_cutoff_date(self):
        from unittest import mock

        from src.fetcher import fetch_cisa_kev

        resp = response(
            json_body={
                "vulnerabilities": [
                    {
                        "cveID": "CVE-2026-1",
                        "vendorProject": "Acme",
                        "product": "Router",
                        "vulnerabilityName": "Acme Router RCE",
                        "dateAdded": "2026-11-16",
                        "shortDescription": "Remote code execution.",
                        "requiredAction": "Patch.",
                        "knownRansomwareCampaignUse": "Known",
                    },
                    {"cveID": "CVE-2026-0", "dateAdded": "2026-11-10"},
                    {"dateAdded": "2026-11-17"},  # malformed: no CVE id
                ]
            }
        )
        with mock.patch("src.fetcher.base.requests.get", return_value=resp):
            items, health = fetch_cisa_kev(
                "https://x/kev.json", datetime(2026, 11, 16, 7, 5, tzinfo=UTC), 2000, 5
            )
        self.assertEqual([i["url"] for i in items], ["https://nvd.nist.gov/vuln/detail/CVE-2026-1"])
        item = items[0]
        self.assertEqual((item["source"], item["source_type"]), ("CISA KEV", "OFFICIAL"))
        self.assertEqual(item["title"], "CVE-2026-1: Acme Router RCE")
        self.assertEqual(item["published_at"], "2026-11-16")
        self.assertIn("evidence of active exploitation", item["content"])
        self.assertIn("ransomware campaigns: Known", item["content"])
        self.assertTrue(health[0]["ok"])
        self.assertEqual(health[0]["items"], 1)

    def test_cisa_kev_failure_is_reported_not_raised(self):
        from unittest import mock

        import requests

        from src.fetcher import fetch_cisa_kev

        with mock.patch(
            "src.fetcher.base.requests.get", side_effect=requests.ConnectionError("down")
        ):
            items, health = fetch_cisa_kev("https://x/kev.json", datetime.now(UTC), 500, 5)
        self.assertEqual(items, [])
        self.assertFalse(health[0]["ok"])

    def test_sitemap_keeps_pages_by_their_own_publication_date(self):
        # Anthropic re-stamps the lastmod of old pages in bulk: only the page's own date counts.
        from unittest import mock

        from src.config_loader import SitemapSource
        from src.fetcher import fetch_sitemaps

        sitemap = response("""<urlset>
          <url><loc>https://a.com/news/new</loc><lastmod>2026-11-20T09:00:00Z</lastmod></url>
          <url><loc>https://a.com/research/old</loc><lastmod>2026-11-20T08:00:00Z</lastmod></url>
          <url><loc>https://a.com/legal/terms</loc><lastmod>2026-11-20T08:00:00Z</lastmod></url>
          <url><loc>https://evil.example/news/x</loc><lastmod>2026-11-20T10:00:00Z</lastmod></url>
          <url><loc>http://a.com/news/plain</loc><lastmod>2026-11-20T10:00:00Z</lastmod></url>
          <url><loc>https://a.com/news/moved</loc><lastmod>2026-11-20T10:00:00Z</lastmod></url>
          <url><loc>https://a.com/news/stale</loc><lastmod>2026-10-01T08:00:00Z</lastmod></url>
        </urlset>""")

        def page(published, title):
            return response(
                f'<meta property="og:title" content="{title}"/>'
                f'<meta property="og:description" content="About &amp; more"/>'
                f'<meta property="article:published_time" content="{published}"/>'
            )

        pages = {
            "https://a.com/sitemap.xml": sitemap,
            "https://a.com/news/new": page("2026-11-20T00:00:00.000Z", "New launch"),
            "https://a.com/research/old": page("2025-02-10T00:00:00.000Z", "Old paper"),
            "https://a.com/news/moved": page("2026-11-20T00:00:00.000Z", "Moved"),
        }
        for u, r in pages.items():
            r.url = u
        pages["https://a.com/news/moved"].url = "https://other.example/landing"  # redirected
        source = SitemapSource(
            name="A News",
            url="https://a.com/sitemap.xml",
            type="OFFICIAL",
            include=["/news/", "/research/"],
        )
        with mock.patch(
            "src.fetcher.base.requests.get", side_effect=lambda url, **_: pages[url]
        ) as get:
            items, health = fetch_sitemaps(
                [source], datetime(2026, 11, 19, 7, 5, tzinfo=UTC), 5, 500, 5
            )
        self.assertEqual([i["url"] for i in items], ["https://a.com/news/new"])
        self.assertEqual(items[0]["title"], "New launch")
        self.assertEqual(items[0]["content"], "About & more")
        self.assertEqual(items[0]["source_type"], "OFFICIAL")
        requested = [c.args[0] for c in get.call_args_list]
        self.assertNotIn("https://a.com/legal/terms", requested)
        # Pages on another host or over plain http are never requested.
        self.assertNotIn("https://evil.example/news/x", requested)
        self.assertNotIn("http://a.com/news/plain", requested)
        self.assertEqual((health[0]["ok"], health[0]["items"], health[0]["kind"]), (True, 1, "rss"))

    def test_huggingface_keeps_models_created_since_the_cutoff(self):
        from unittest import mock

        from src.config_loader import HuggingFaceAuthor
        from src.fetcher import fetch_huggingface_models

        listing = response(
            json_body=[
                {
                    "id": "lab/Model-2",
                    "createdAt": "2026-11-20T03:00:00.000Z",
                    "pipeline_tag": "text-generation",
                },
                {"id": "lab/Model-1", "createdAt": "2026-10-01T03:00:00.000Z"},
                {"id": "someone-else/Fake", "createdAt": "2026-11-20T04:00:00.000Z"},
                {"id": "lab/../../evil", "createdAt": "2026-11-20T04:00:00.000Z"},
                {"id": "lab/a b?x=1", "createdAt": "2026-11-20T04:00:00.000Z"},
            ]
        )
        card = response("---\nlicense: mit\n---\n# Model 2\n![logo](a.png) A [fast](x) model.")

        def get(url, **_):
            return card if url.endswith("README.md") else listing

        with mock.patch("src.fetcher.base.requests.get", side_effect=get):
            items, health = fetch_huggingface_models(
                [HuggingFaceAuthor(name="Lab", author="lab")],
                datetime(2026, 11, 19, 7, 5, tzinfo=UTC),
                5,
                500,
                5,
            )
        self.assertEqual([i["url"] for i in items], ["https://huggingface.co/lab/Model-2"])
        item = items[0]
        self.assertEqual((item["source"], item["source_type"]), ("Hugging Face · Lab", "OFFICIAL"))
        self.assertEqual(item["title"], "Lab releases Model-2 on Hugging Face")
        self.assertIn("(text-generation). Model 2 A fast model.", item["content"])
        self.assertNotIn("license", item["content"])
        self.assertEqual(health[0]["items"], 1)

    def test_huggingface_failure_is_reported_not_raised(self):
        from unittest import mock

        import requests

        from src.config_loader import HuggingFaceAuthor
        from src.fetcher import fetch_huggingface_models

        with mock.patch(
            "src.fetcher.base.requests.get", side_effect=requests.ConnectionError("down")
        ):
            items, health = fetch_huggingface_models(
                [HuggingFaceAuthor(name="Lab", author="lab")], datetime.now(UTC), 5, 500, 5
            )
        self.assertEqual(items, [])
        self.assertFalse(health[0]["ok"])

    def test_oversized_response_is_refused(self):
        from unittest import mock

        from src.fetcher.base import MAX_RESPONSE_BYTES, ResponseTooLarge
        from src.fetcher.base import http_get as _get

        big = response("x" * (MAX_RESPONSE_BYTES + 1))
        with (
            mock.patch("src.fetcher.base.requests.get", return_value=big),
            self.assertRaises(ResponseTooLarge),
        ):
            _get("https://a.com/feed", timeout=5)
        declared = response("x", headers={"Content-Length": str(MAX_RESPONSE_BYTES + 1)})
        with (
            mock.patch("src.fetcher.base.requests.get", return_value=declared),
            self.assertRaises(ResponseTooLarge),
        ):
            _get("https://a.com/feed", timeout=5)


if __name__ == "__main__":
    unittest.main()
