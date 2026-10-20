from __future__ import annotations

import json
from http.server import ThreadingHTTPServer
from pathlib import Path
import tempfile
from threading import Thread
import unittest
from datetime import datetime, timezone
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch

from collect import collect
from review import (
    build_dashboard,
    current_status,
    filter_and_sort_articles,
    load_decisions,
    publication_eligible,
    record_decision,
)
from review_server import create_server


def article(article_id: str, **overrides: object) -> dict[str, object]:
    return {
        "stable_id": article_id,
        "title": f"Research story {article_id}",
        "publisher": "Test Publisher",
        "source_id": "test-publisher",
        "canonical_url": "https://example.test/article",
        "source_url": "https://example.test/article",
        "feed_url": "https://example.test/feed.xml",
        "published_at": "2026-10-19T12:00:00Z",
        "retrieved_at": "2026-10-19T13:00:00Z",
        "category": "data_centers",
        "affected_tickers": ["NVDA"],
        "mentioned_companies": ["NVIDIA"],
        "excerpt": "A source feed excerpt.",
        "relevance_score": 88,
        "priority": "high",
        "relevance_reasons": ["Relevant data centers topic (+35)."],
        "event_type": "investment_or_funding",
        "review_status": "unreviewed",
        **overrides,
    }


class EditorialDecisionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.queue_path = self.root / "research-queue.json"
        self.decisions_path = self.root / "editorial-decisions.json"
        self.write_queue([article("publisher:story-1")])

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def write_queue(self, items: list[dict[str, object]]) -> None:
        self.queue_path.write_text(
            json.dumps({"schema_version": 2, "items": items, "excluded_items": []}),
            encoding="utf-8",
        )

    def record(self, decision: str, request_id: str, notes: str = "") -> bool:
        return record_decision(
            "publisher:story-1",
            decision,
            "Editorial Reviewer",
            notes,
            request_id,
            queue_path=self.queue_path,
            decisions_path=self.decisions_path,
            timestamp=datetime(2026, 10, 19, 14, tzinfo=timezone.utc),
        )

    def test_decision_persistence_and_auditable_status_transitions(self) -> None:
        self.assertEqual(current_status(article("publisher:story-1"), []), "awaiting_review")
        self.assertFalse(publication_eligible(article("publisher:story-1"), []))
        with self.assertRaisesRegex(ValueError, "already been reviewed"):
            self.record("reconsider", "request-0")
        self.assertTrue(self.record("approve", "request-1", "Verified the original source."))
        self.assertTrue(self.record("hold", "request-2", "Waiting for follow-up."))
        self.assertTrue(self.record("reconsider", "request-3"))
        self.assertTrue(self.record("reject", "request-4", "Insufficient source detail."))
        records = load_decisions(self.decisions_path)
        self.assertEqual(len(records), 4)
        self.assertEqual([record["decision"] for record in records], ["approve", "hold", "reconsider", "reject"])
        self.assertEqual(records[0]["reviewer"], "Editorial Reviewer")
        self.assertEqual(records[0]["notes"], "Verified the original source.")
        self.assertEqual(records[0]["timestamp"], "2026-10-19T14:00:00Z")
        self.assertEqual(records[-1]["status"], "rejected")
        self.assertEqual(current_status(article("publisher:story-1"), records), "rejected")
        self.assertFalse(publication_eligible(article("publisher:story-1"), records))
        self.record("approve", "request-5")
        self.assertTrue(publication_eligible(
            article("publisher:story-1"), load_decisions(self.decisions_path)
        ))

    def test_duplicate_requests_are_idempotent_and_duplicate_queue_ids_render_once(self) -> None:
        self.record("approve", "same-request")
        contents = self.decisions_path.read_bytes()
        self.assertFalse(self.record("approve", "same-request"))
        self.assertEqual(self.decisions_path.read_bytes(), contents)
        self.write_queue([article("publisher:story-1"), article("publisher:story-1", title="Duplicate")])
        payload = build_dashboard(self.queue_path, self.decisions_path)
        self.assertEqual(len(payload["items"]), 1)
        self.assertEqual(payload["counts"]["approved"], 1)

    def test_legacy_queue_review_is_visible_and_migrated_before_new_decisions(self) -> None:
        self.write_queue([
            article(
                "publisher:story-1",
                review_status="approved",
                reviewed_by="Legacy Reviewer",
                reviewed_at="2026-10-18T09:30:00Z",
            )
        ])
        legacy = build_dashboard(self.queue_path, self.decisions_path)["items"][0]["decision_history"]
        self.assertEqual(legacy[0]["source"], "legacy_queue")
        self.assertEqual(legacy[0]["reviewer"], "Legacy Reviewer")
        self.record("reject", "after-migration")
        records = load_decisions(self.decisions_path)
        self.assertEqual([record["decision"] for record in records], ["approve", "reject"])
        self.assertEqual(records[0]["reviewer"], "Legacy Reviewer")

    def test_rejects_unknown_articles_invalid_input_and_reviewer_metadata(self) -> None:
        with self.assertRaisesRegex(ValueError, "not in the active review queue"):
            record_decision(
                "missing:article", "approve", "Reviewer", "", "request",
                queue_path=self.queue_path, decisions_path=self.decisions_path,
            )
        with self.assertRaisesRegex(ValueError, "must be text"):
            record_decision(
                "publisher:story-1", "approve", "Reviewer", [], "request",  # type: ignore[arg-type]
                queue_path=self.queue_path, decisions_path=self.decisions_path,
            )
        with self.assertRaisesRegex(ValueError, "at most 2000"):
            self.record("reject", "large-note", "x" * 2001)

    def test_filtering_and_score_and_publication_date_sorting(self) -> None:
        items = [
            article("older", published_at="2026-10-17T12:00:00Z", relevance_score=72, priority="medium"),
            article("newer", published_at="2026-10-19T12:00:00Z", relevance_score=91),
            article("other", category="robotics", affected_tickers=["AMD"], mentioned_companies=["Advanced Micro Devices"], relevance_score=45, priority="low"),
        ]
        self.assertEqual(
            [item["stable_id"] for item in filter_and_sort_articles(items, priority="high")],
            ["newer"],
        )
        filtered = filter_and_sort_articles(
            items, query="nvidia", category="data_centers", company="NVDA",
            status="awaiting_review", sort_by="score_desc",
        )
        self.assertEqual([item["stable_id"] for item in filtered], ["newer", "older"])
        self.assertEqual(
            [item["stable_id"] for item in filter_and_sort_articles(items, sort_by="date_asc")],
            ["older", "other", "newer"],
        )
        self.assertEqual(
            [item["stable_id"] for item in filter_and_sort_articles(items, sort_by="score_asc")],
            ["other", "older", "newer"],
        )
        self.assertEqual(
            [item["stable_id"] for item in filter_and_sort_articles(items, company="AMD")],
            ["other"],
        )

    def test_decision_log_is_preserved_across_a_subsequent_collection(self) -> None:
        source_path = self.root / "sources.json"
        source_path.write_text(
            json.dumps([{
                "id": "test-publisher",
                "publisher": "Test Publisher",
                "url": "https://feeds.example.test/rss.xml",
                "source_type": "publisher",
            }]),
            encoding="utf-8",
        )
        feed = b"""<rss><channel><item>
          <title>New semiconductor capacity investment</title>
          <link>https://example.test/semiconductor-capacity</link>
          <guid>capacity-1</guid>
          <pubDate>Mon, 19 Oct 2026 10:30:00 GMT</pubDate>
          <description>New semiconductor manufacturing capacity and AI compute investment.</description>
        </item></channel></rss>"""
        with patch("collect.fetch_feed", return_value=feed):
            self.assertTrue(collect(source_path, self.queue_path, datetime(2026, 10, 19, 11, tzinfo=timezone.utc)))
            stable_id = json.loads(self.queue_path.read_text(encoding="utf-8"))["items"][0]["stable_id"]
            record_decision(
                stable_id, "approve", "Reviewer", "Checked source.", "collection-preservation",
                queue_path=self.queue_path, decisions_path=self.decisions_path,
            )
            decisions_before = self.decisions_path.read_bytes()
            self.assertFalse(collect(source_path, self.queue_path, datetime(2026, 10, 20, 11, tzinfo=timezone.utc)))
        self.assertEqual(self.decisions_path.read_bytes(), decisions_before)
        self.assertEqual(build_dashboard(self.queue_path, self.decisions_path)["items"][0]["review_status"], "approved")


class LocalWriteSecurityTests(unittest.TestCase):
    def test_local_api_requires_token_and_same_origin_for_writes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            queue = root / "queue.json"
            decisions = root / "decisions.json"
            queue.write_text(json.dumps({"schema_version": 2, "items": [article("p:1")]}), encoding="utf-8")
            server: ThreadingHTTPServer = create_server(
                queue, decisions, reviewer="Local Reviewer", token="session-token", port=0
            )
            thread = Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base_url = f"http://127.0.0.1:{server.server_address[1]}"
            try:
                with urlopen(f"{base_url}/") as response:
                    page = response.read().decode("utf-8")
                    self.assertIn('nonce="', page)
                    self.assertNotIn("__REVIEW_TOKEN__", page)
                    self.assertIn("script-src 'nonce-", response.headers["Content-Security-Policy"])
                with self.assertRaises(HTTPError) as unauthorized:
                    urlopen(f"{base_url}/api/dashboard")
                self.assertEqual(unauthorized.exception.code, 403)

                body = json.dumps({"article_id": "p:1", "decision": "approve", "notes": ""}).encode()
                for origin in ("https://attacker.example", f"http://attacker.example:{server.server_address[1]}"):
                    request = Request(
                        f"{base_url}/api/decisions",
                        data=body,
                        headers={
                            "Content-Type": "application/json",
                            "Origin": origin,
                            "X-Review-Token": "session-token",
                        },
                    )
                    with self.assertRaises(HTTPError) as forbidden:
                        urlopen(request)
                    self.assertEqual(forbidden.exception.code, 403)

                request = Request(
                    f"{base_url}/api/decisions",
                    data=body,
                    headers={
                        "Content-Type": "application/json",
                        "Origin": base_url,
                        "X-Review-Token": "session-token",
                    },
                )
                with urlopen(request) as response:
                    self.assertEqual(response.status, 200)
                self.assertEqual(load_decisions(decisions)[0]["reviewer"], "Local Reviewer")
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_review_controls_and_data_are_outside_astro_public_routes(self) -> None:
        repository = Path(__file__).resolve().parent.parent
        pages = repository / "src" / "pages"
        routes = list(pages.rglob("*"))
        route_text = "\n".join(
            path.read_text(encoding="utf-8")
            for path in routes
            if path.is_file() and path.suffix in {".astro", ".ts", ".js"}
        )
        self.assertNotIn("editorial-decisions.json", route_text)
        self.assertNotIn("research-queue.json", route_text)
        self.assertTrue((repository / "research" / "review-dashboard.html").is_file())
        self.assertTrue((repository / "research" / "data" / "editorial-decisions.json").is_file())
        self.assertFalse((pages / "review.astro").exists())

    def test_github_writes_are_dispatch_authenticated_and_tokens_stay_server_side(self) -> None:
        repository = Path(__file__).resolve().parent.parent
        workflow = (repository / ".github" / "workflows" / "editorial-review.yml").read_text(
            encoding="utf-8"
        )
        dashboard = (repository / "research" / "review-dashboard.html").read_text(
            encoding="utf-8"
        )
        self.assertIn("workflow_dispatch:", workflow)
        self.assertIn("contents: write", workflow)
        self.assertIn("pull-requests: write", workflow)
        self.assertIn("REVIEWER: ${{ github.actor }}", workflow)
        self.assertIn("GH_TOKEN: ${{ github.token }}", workflow)
        self.assertNotIn("GITHUB_TOKEN", dashboard)
        self.assertNotIn("GH_TOKEN", dashboard)


if __name__ == "__main__":
    unittest.main()
