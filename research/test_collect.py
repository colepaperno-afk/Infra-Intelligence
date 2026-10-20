from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import patch
from urllib.error import URLError

from collect import (
    FeedError,
    canonicalize_url,
    classify_entry,
    collect,
    deduplicate_items,
    fetch_feed,
    load_queue,
    parse_feed,
    score_article,
    validate_source_registry,
)


SOURCE = {
    "id": "test-publisher",
    "publisher": "Test Publisher",
    "url": "https://feeds.example.test/rss.xml",
    "source_type": "publisher",
}
RETRIEVED_AT = "2026-10-19T00:00:00Z"


class ParseFeedTests(unittest.TestCase):
    def test_parses_rss_normalizes_timestamp_excerpt_and_ticker(self) -> None:
        payload = b"""<?xml version="1.0"?>
        <rss version="2.0"><channel><item>
          <title>AMD announces new AI accelerator</title>
          <link>https://example.test/story?utm_source=feed</link>
          <guid isPermaLink="false">story-42</guid>
          <pubDate>Mon, 19 Oct 2026 10:30:00 -0400</pubDate>
          <description><![CDATA[<p>AMD describes a new accelerator.</p><script>discard this</script>]]></description>
        </item></channel></rss>"""
        items = parse_feed(payload, SOURCE, RETRIEVED_AT)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["title"], "AMD announces new AI accelerator")
        self.assertEqual(items[0]["publisher"], SOURCE["publisher"])
        self.assertEqual(items[0]["source_url"], "https://example.test/story?utm_source=feed")
        self.assertEqual(items[0]["feed_url"], SOURCE["url"])
        self.assertEqual(items[0]["published_at"], "2026-10-19T14:30:00Z")
        self.assertEqual(items[0]["retrieved_at"], RETRIEVED_AT)
        self.assertEqual(items[0]["category"], "ai_semiconductors")
        self.assertEqual(items[0]["affected_tickers"], ["AMD"])
        self.assertEqual(items[0]["excerpt"], "AMD describes a new accelerator.")
        self.assertEqual(items[0]["review_status"], "unreviewed")
        self.assertIn(items[0]["priority"], {"high", "medium", "low", "excluded"})
        self.assertIsInstance(items[0]["relevance_score"], int)
        self.assertIsInstance(items[0]["relevance_reasons"], list)
        self.assertEqual(items[0]["mentioned_companies"], ["Advanced Micro Devices"])

    def test_parses_atom_feed_and_alternate_link(self) -> None:
        payload = b"""<feed xmlns="http://www.w3.org/2005/Atom">
          <entry><id>tag:example.test,2026:9</id><title>New optical interconnect</title>
            <link rel="alternate" href="https://example.test/optical"/>
            <updated>2026-10-19T12:00:00-04:00</updated>
            <summary>Networking equipment launch.</summary>
          </entry>
        </feed>"""
        items = parse_feed(payload, SOURCE, RETRIEVED_AT)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["category"], "networking")
        self.assertEqual(items[0]["published_at"], "2026-10-19T16:00:00Z")

    def test_rejects_invalid_or_missing_entry_urls_and_timestamps(self) -> None:
        payload = b"""<rss><channel>
          <item><title>AI chip</title><link>javascript:alert(1)</link>
            <pubDate>Mon, 19 Oct 2026 10:30:00 GMT</pubDate></item>
          <item><title>AI accelerator</title><link>https://example.test/no-date</link></item>
        </channel></rss>"""
        with self.assertLogs("infra_research", level="WARNING") as captured:
            self.assertEqual(parse_feed(payload, SOURCE, RETRIEVED_AT), [])
        self.assertEqual(len(captured.output), 2)

    def test_empty_feed_is_valid(self) -> None:
        self.assertEqual(parse_feed(b"<rss><channel /></rss>", SOURCE, RETRIEVED_AT), [])

    def test_invalid_xml_fails_explicitly(self) -> None:
        with self.assertRaises(FeedError):
            parse_feed(b"<rss><item>", SOURCE, RETRIEVED_AT)


class URLAndRegistryTests(unittest.TestCase):
    def test_canonical_url_strips_tracking_and_normalizes_host(self) -> None:
        self.assertEqual(
            canonicalize_url("HTTPS://Example.TEST:443/story/?utm_campaign=a&b=2#part"),
            "https://example.test/story?b=2",
        )

    def test_rejects_missing_and_non_http_urls(self) -> None:
        for value in (
            "",
            "javascript:alert(1)",
            "https:///missing-host",
            "https://user:pass@example.test",
            "https://example.test/bad%escape",
            "https://example.test/has space",
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                canonicalize_url(value)

    def test_classification_does_not_force_source_fallback_category(self) -> None:
        payload = b"""<rss><channel><item>
          <title>NVIDIA updates quarterly meeting schedule</title>
          <link>https://example.test/meeting</link>
          <guid>meeting-1</guid>
          <pubDate>Mon, 19 Oct 2026 10:30:00 GMT</pubDate>
        </item></channel></rss>"""
        source = {**SOURCE, "fallback_category": "ai_semiconductors"}
        item = parse_feed(payload, source, RETRIEVED_AT)[0]
        self.assertEqual(item["category"], "other")
        self.assertEqual(item["relevance_score"], 0)
        self.assertEqual(item["priority"], "excluded")

    def test_classifies_all_covered_infrastructure_categories(self) -> None:
        examples = {
            "ai_semiconductors": "New semiconductor GPU accelerator",
            "networking": "Optical networking and Ethernet capacity",
            "data_centers": "Data-center campus expansion",
            "energy_power": "Power infrastructure and energy generation",
            "cooling": "Liquid cooling for high-density systems",
            "robotics": "Industrial robotics deployment",
        }
        for category, text in examples.items():
            with self.subTest(category=category):
                self.assertEqual(classify_entry(text), category)

    def test_source_registry_requires_valid_unique_urls(self) -> None:
        with self.assertRaises(ValueError):
            validate_source_registry([{**SOURCE, "url": ""}])
        with self.assertRaises(ValueError):
            validate_source_registry([SOURCE, SOURCE])


class RelevanceScoringTests(unittest.TestCase):
    def _score(
        self,
        title: str,
        excerpt: str,
        category: str,
        published_at: str = "2026-10-18T00:00:00Z",
    ) -> dict[str, object]:
        from collect import identify_companies

        companies, _ = identify_companies(f"{title} {excerpt}")
        return score_article(
            title,
            excerpt,
            published_at,
            RETRIEVED_AT,
            category,
            companies,
        )

    def test_high_priority_investment_and_capacity_announcement(self) -> None:
        result = self._score(
            "NVIDIA commits $10 billion to build a 5 GW AI data center campus",
            "Investment expands AI compute capacity for cloud infrastructure.",
            "data_centers",
        )
        self.assertEqual(result["relevance_score"], 100)
        self.assertEqual(result["priority"], "high")
        self.assertEqual(result["event_type"], "investment_or_funding")
        self.assertGreaterEqual(len(result["relevance_reasons"]), 4)

    def test_company_mention_alone_does_not_qualify_and_gaming_is_excluded(self) -> None:
        result = self._score(
            "NVIDIA launches a GeForce GPU for video game players",
            "Gaming graphics news for console and esports fans.",
            "other",
        )
        self.assertEqual(result["priority"], "excluded")
        self.assertEqual(result["relevance_score"], 0)
        self.assertIn("gaming", result["exclusion_reason"])
        company_only = self._score(
            "NVIDIA updates quarterly meeting schedule",
            "A routine shareholder notice.",
            "other",
        )
        self.assertEqual(company_only["relevance_score"], 0)
        self.assertEqual(company_only["priority"], "excluded")

    def test_sponsored_content_is_excluded(self) -> None:
        result = self._score(
            "Sponsored: NVIDIA launches AI data center accelerator",
            "Paid promotional content covering AI compute capacity.",
            "ai_semiconductors",
        )
        self.assertEqual(result["priority"], "excluded")
        self.assertIn("sponsored", result["exclusion_reason"])

    def test_outdated_content_is_excluded_and_evergreen_content_is_low(self) -> None:
        stale = self._score(
            "Data center investment builds compute capacity",
            "A $2 billion facility adds cloud servers.",
            "data_centers",
            "2024-10-18T00:00:00Z",
        )
        evergreen = self._score(
            "How to understand data centers",
            "An evergreen guide to cloud infrastructure.",
            "data_centers",
        )
        self.assertEqual(stale["priority"], "excluded")
        self.assertIn("days", stale["exclusion_reason"])
        self.assertEqual(evergreen["priority"], "low")
        self.assertEqual(evergreen["relevance_score"], 40)

    def test_robotics_battery_story_remains_robotics(self) -> None:
        title = "Robotics startup unveils battery-charging humanoid fleet"
        excerpt = "Robots use autonomous charging for warehouse deployment."
        self.assertEqual(classify_entry(f"{title} {excerpt}"), "robotics")
        result = self._score(title, excerpt, "robotics")
        self.assertEqual(result["priority"], "low")
        self.assertNotIn("energy", " ".join(result["relevance_reasons"]).lower())

    def test_energy_needs_compute_infrastructure_connection(self) -> None:
        relevant = self._score(
            "Utility signs nuclear agreement to serve AI data center campus",
            "New generation supports 1 GW of compute demand.",
            "energy_power",
        )
        unrelated = self._score(
            "Utility announces regional nuclear energy program",
            "New generation capacity supports local communities.",
            "energy_power",
        )
        self.assertGreaterEqual(relevant["relevance_score"], 80)
        self.assertEqual(relevant["priority"], "high")
        self.assertEqual(unrelated["priority"], "excluded")
        self.assertIn("meaningful link", unrelated["exclusion_reason"])


class DeduplicationTests(unittest.TestCase):
    def _item(self, stable_id: str, url: str, date: str, **extra: object) -> dict[str, object]:
        return {
            "stable_id": stable_id,
            "canonical_url": canonicalize_url(url),
            "published_at": date,
            "review_status": "unreviewed",
            **extra,
        }

    def test_deduplicates_by_canonical_url_and_stable_id_preserving_review(self) -> None:
        approved = self._item(
            "publisher:guid-1",
            "https://example.test/story?utm_source=old",
            "2026-10-18T00:00:00Z",
            review_status="approved",
        )
        duplicate_url = self._item(
            "other:guid-2",
            "https://example.test/story",
            "2026-10-19T00:00:00Z",
        )
        duplicate_id = self._item(
            "publisher:guid-1",
            "https://example.test/new-url",
            "2026-10-19T00:00:00Z",
        )
        items = deduplicate_items([approved], [duplicate_url, duplicate_id])
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["review_status"], "approved")

    def test_sorts_deterministically_and_enforces_retention(self) -> None:
        items = [
            self._item("a", "https://example.test/a", "2026-10-17T00:00:00Z"),
            self._item("c", "https://example.test/c", "2026-10-19T00:00:00Z"),
            self._item("b", "https://example.test/b", "2026-10-18T00:00:00Z"),
        ]
        result = deduplicate_items([], items, retention_limit=2)
        self.assertEqual([item["stable_id"] for item in result], ["c", "b"])
        self.assertEqual(
            result,
            deduplicate_items([], list(reversed(items)), retention_limit=2),
        )
        with self.assertRaises(ValueError):
            deduplicate_items([], items, retention_limit=0)


class CollectionFailureTests(unittest.TestCase):
    def test_retries_transient_fetch_failure(self) -> None:
        class Response:
            def __enter__(self) -> Response:
                return self

            def __exit__(self, *args: object) -> None:
                return None

            def read(self, size: int) -> bytes:
                return b"<rss />"

        with patch("collect.urlopen", side_effect=[URLError("temporary"), Response()]) as opener:
            with patch("collect.time.sleep") as sleeper:
                self.assertEqual(fetch_feed(SOURCE), b"<rss />")
        self.assertEqual(opener.call_count, 2)
        sleeper.assert_called_once_with(1)

    def test_all_sources_failed_leaves_existing_queue_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            registry = root / "sources.json"
            output = root / "queue.json"
            existing = {"schema_version": 2, "items": [], "excluded_items": []}
            output.write_text(json.dumps(existing), encoding="utf-8")
            registry.write_text(json.dumps([SOURCE]), encoding="utf-8")
            with patch("collect.fetch_feed", side_effect=FeedError("offline")):
                with self.assertRaisesRegex(FeedError, "All configured sources failed"):
                    collect(registry, output, datetime(2026, 10, 19, tzinfo=timezone.utc))
            self.assertEqual(json.loads(output.read_text(encoding="utf-8")), existing)

    def test_successful_empty_collection_does_not_rewrite_queue(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            registry = root / "sources.json"
            output = root / "queue.json"
            existing = {"schema_version": 2, "items": [], "excluded_items": []}
            serialized = json.dumps(existing, indent=2, sort_keys=True) + "\n"
            output.write_text(serialized, encoding="utf-8")
            registry.write_text(json.dumps([SOURCE]), encoding="utf-8")
            with patch("collect.fetch_feed", return_value=b"<rss><channel/></rss>"):
                changed = collect(registry, output, datetime(2026, 10, 19, tzinfo=timezone.utc))
            self.assertFalse(changed)
            self.assertEqual(output.read_text(encoding="utf-8"), serialized)

    def test_excluded_items_are_archived_outside_active_review_queue(self) -> None:
        feed = b"""<rss><channel><item>
          <title>Gaming GPU launch for video game players</title>
          <link>https://example.test/gaming-gpu</link>
          <guid>gaming-1</guid>
          <pubDate>Mon, 19 Oct 2026 10:30:00 GMT</pubDate>
          <description>GeForce graphics for esports and console gaming.</description>
        </item></channel></rss>"""
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            registry = root / "sources.json"
            output = root / "queue.json"
            registry.write_text(json.dumps([SOURCE]), encoding="utf-8")
            with patch("collect.fetch_feed", return_value=feed):
                self.assertTrue(
                    collect(registry, output, datetime(2026, 10, 19, 11, tzinfo=timezone.utc))
                )
                document = json.loads(output.read_text(encoding="utf-8"))
                self.assertEqual(document["items"], [])
                self.assertEqual(len(document["excluded_items"]), 1)
                self.assertEqual(document["excluded_items"][0]["priority"], "excluded")
                self.assertIn("gaming", document["excluded_items"][0]["exclusion_reason"])

    def test_new_findings_are_persisted_once_and_review_decision_is_preserved(self) -> None:
        feed = b"""<rss><channel><item>
          <title>New semiconductor capacity</title>
          <link>https://example.test/semiconductor-capacity</link>
          <guid>capacity-1</guid>
          <pubDate>Mon, 19 Oct 2026 10:30:00 GMT</pubDate>
          <description>New semiconductor manufacturing capacity.</description>
        </item></channel></rss>"""
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            registry = root / "sources.json"
            output = root / "queue.json"
            registry.write_text(json.dumps([SOURCE]), encoding="utf-8")
            with patch("collect.fetch_feed", return_value=feed):
                self.assertTrue(
                    collect(
                        registry,
                        output,
                        datetime(2026, 10, 19, 11, tzinfo=timezone.utc),
                    )
                )
                finding = load_queue(output)[0]
                self.assertEqual(finding["review_status"], "unreviewed")
                finding["review_status"] = "approved"
                finding["reviewed_by"] = "reviewer"
                finding["reviewed_at"] = "2026-10-19T12:00:00Z"
                output.write_text(
                    json.dumps(
                        {"schema_version": 2, "items": [finding], "excluded_items": []},
                        ensure_ascii=False,
                        indent=2,
                        sort_keys=True,
                    )
                    + "\n",
                    encoding="utf-8",
                )
                approved_contents = output.read_text(encoding="utf-8")
                self.assertFalse(
                    collect(
                        registry,
                        output,
                        datetime(2026, 10, 20, 11, tzinfo=timezone.utc),
                    )
                )
            self.assertEqual(output.read_text(encoding="utf-8"), approved_contents)

    def test_invalid_existing_queue_is_not_silently_discarded(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            output = Path(temporary_directory) / "queue.json"
            output.write_text(
                json.dumps({"schema_version": 1, "items": [{"stable_id": "broken"}]}),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "missing required field"):
                load_queue(output)


if __name__ == "__main__":
    unittest.main()
