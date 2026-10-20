#!/usr/bin/env python3
"""Editorial review storage and queue operations for the local review dashboard."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
from typing import Any


ROOT = Path(__file__).resolve().parent
DEFAULT_QUEUE = ROOT / "data" / "research-queue.json"
DEFAULT_DECISIONS = ROOT / "data" / "editorial-decisions.json"
DECISION_STATUSES = {
    "approve": "approved",
    "reject": "rejected",
    "hold": "on_hold",
    "reconsider": "awaiting_review",
}
LEGACY_STATUSES = {
    "unreviewed": "awaiting_review",
    "awaiting_review": "awaiting_review",
    "approved": "approved",
    "rejected": "rejected",
    "on_hold": "on_hold",
}
MAX_NOTES_LENGTH = 2000
MAX_REVIEWER_LENGTH = 120


def read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Could not read {path}: {exc}") from exc


def load_decisions(path: Path = DEFAULT_DECISIONS) -> list[dict[str, Any]]:
    payload = read_json(path, {"schema_version": 1, "decisions": []})
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != 1
        or not isinstance(payload.get("decisions"), list)
    ):
        raise ValueError(f"Invalid editorial decision store: {path}")

    seen_requests: set[tuple[str, str]] = set()
    for number, record in enumerate(payload["decisions"], start=1):
        if not isinstance(record, dict):
            raise ValueError(f"Editorial decision {number} must be an object")
        for field in ("article_id", "decision", "status", "timestamp", "reviewer", "request_id"):
            if not isinstance(record.get(field), str) or not record[field].strip():
                raise ValueError(f"Editorial decision {number} requires '{field}'")
        if record["decision"] not in DECISION_STATUSES:
            raise ValueError(f"Editorial decision {number} has an invalid decision")
        if record["status"] != DECISION_STATUSES[record["decision"]]:
            raise ValueError(f"Editorial decision {number} has an inconsistent status")
        try:
            parsed = datetime.fromisoformat(record["timestamp"].replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"Editorial decision {number} has an invalid timestamp") from exc
        if parsed.tzinfo is None:
            raise ValueError(f"Editorial decision {number} timestamp must include a timezone")
        if len(record["reviewer"]) > MAX_REVIEWER_LENGTH:
            raise ValueError(f"Editorial decision {number} reviewer is too long")
        notes = record.get("notes", "")
        if not isinstance(notes, str) or len(notes) > MAX_NOTES_LENGTH:
            raise ValueError(f"Editorial decision {number} has invalid notes")
        request_key = (record["article_id"], record["request_id"])
        if request_key in seen_requests:
            raise ValueError(f"Duplicate editorial request id for {record['article_id']}")
        seen_requests.add(request_key)
    return payload["decisions"]


def current_status(article: dict[str, Any], decisions: list[dict[str, Any]]) -> str:
    article_id = article.get("stable_id")
    for decision in reversed(decisions):
        if decision["article_id"] == article_id:
            return decision["status"]
    return LEGACY_STATUSES.get(article.get("review_status", "unreviewed"), "awaiting_review")


def publication_eligible(article: dict[str, Any], decisions: list[dict[str, Any]]) -> bool:
    """Keep publication eligibility independent from relevance and legacy queue fields."""
    latest = next(
        (
            record
            for record in reversed(decisions)
            if record["article_id"] == article.get("stable_id")
        ),
        None,
    )
    return latest is not None and latest["decision"] == "approve" and latest["status"] == "approved"


def build_dashboard(queue_path: Path, decisions_path: Path) -> dict[str, Any]:
    queue = read_json(queue_path, {"schema_version": 2, "items": []})
    if not isinstance(queue, dict) or not isinstance(queue.get("items"), list):
        raise ValueError(f"Invalid research queue: {queue_path}")
    decisions = load_decisions(decisions_path)

    unique: dict[str, dict[str, Any]] = {}
    for item in queue["items"]:
        if not isinstance(item, dict) or not isinstance(item.get("stable_id"), str):
            raise ValueError("Research queue contains an item without a stable_id")
        unique.setdefault(item["stable_id"], item)
    items = []
    for item in unique.values():
        enriched = dict(item)
        enriched["review_status"] = current_status(item, decisions)
        history = [
            record for record in decisions if record["article_id"] == item["stable_id"]
        ]
        if not history and item.get("review_status") in {"approved", "rejected"}:
            history.append(
                {
                    "article_id": item["stable_id"],
                    "decision": item["review_status"],
                    "status": LEGACY_STATUSES[item["review_status"]],
                    "timestamp": item.get("reviewed_at", ""),
                    "reviewer": item.get("reviewed_by", "Not provided"),
                    "notes": "",
                    "source": "legacy_queue",
                }
            )
        enriched["decision_history"] = history
        items.append(enriched)

    counts = {
        "awaiting_review": sum(item["review_status"] == "awaiting_review" for item in items),
        "high_priority": sum(
            item["review_status"] == "awaiting_review" and item.get("priority") == "high"
            for item in items
        ),
        "approved": sum(item["review_status"] == "approved" for item in items),
        "rejected": sum(item["review_status"] == "rejected" for item in items),
        "on_hold": sum(item["review_status"] == "on_hold" for item in items),
    }
    return {"items": items, "counts": counts}


def filter_and_sort_articles(
    articles: list[dict[str, Any]],
    *,
    query: str = "",
    category: str = "all",
    priority: str = "all",
    company: str = "all",
    status: str = "all",
    sort_by: str = "score_desc",
) -> list[dict[str, Any]]:
    needle = query.casefold().strip()

    def searchable(article: dict[str, Any]) -> str:
        values = [
            article.get("stable_id", ""),
            article.get("title", ""),
            article.get("publisher", ""),
            article.get("category", ""),
            article.get("event_type", ""),
            article.get("excerpt", ""),
            *article.get("mentioned_companies", []),
            *article.get("affected_tickers", []),
        ]
        return " ".join(str(value) for value in values).casefold()

    selected = [
        article
        for article in articles
        if (not needle or needle in searchable(article))
        and (category == "all" or article.get("category") == category)
        and (priority == "all" or article.get("priority") == priority)
        and (company == "all" or company.casefold() in {
            str(value).casefold()
            for value in [
                *article.get("mentioned_companies", []),
                *article.get("affected_tickers", []),
            ]
        })
        and (
            status == "all"
            or LEGACY_STATUSES.get(article.get("review_status", "unreviewed"), "awaiting_review")
            == status
        )
    ]
    if sort_by == "score_desc":
        selected.sort(key=lambda item: (item.get("relevance_score", 0), item.get("published_at", "")), reverse=True)
    elif sort_by == "score_asc":
        selected.sort(key=lambda item: (item.get("relevance_score", 0), item.get("published_at", "")))
    elif sort_by == "date_desc":
        selected.sort(key=lambda item: (item.get("published_at", ""), item.get("relevance_score", 0)), reverse=True)
    elif sort_by == "date_asc":
        selected.sort(key=lambda item: (item.get("published_at", ""), item.get("relevance_score", 0)))
    else:
        raise ValueError(f"Unsupported sort order: {sort_by}")
    return selected


def record_decision(
    article_id: str,
    decision: str,
    reviewer: str,
    notes: str,
    request_id: str,
    *,
    queue_path: Path = DEFAULT_QUEUE,
    decisions_path: Path = DEFAULT_DECISIONS,
    timestamp: datetime | None = None,
) -> bool:
    if not isinstance(decision, str) or decision not in DECISION_STATUSES:
        raise ValueError("Decision must be approve, reject, hold, or reconsider")
    if not all(isinstance(value, str) for value in (article_id, reviewer, notes, request_id)):
        raise ValueError("Article ID, reviewer, notes, and request ID must be text")
    article_id = article_id.strip()
    reviewer = reviewer.strip()
    notes = notes.strip()
    request_id = request_id.strip()
    if not article_id or len(article_id) > 1000:
        raise ValueError("Article ID is required and must be at most 1000 characters")
    if not reviewer or len(reviewer) > MAX_REVIEWER_LENGTH:
        raise ValueError(f"Reviewer is required and must be at most {MAX_REVIEWER_LENGTH} characters")
    if len(notes) > MAX_NOTES_LENGTH:
        raise ValueError(f"Notes must be at most {MAX_NOTES_LENGTH} characters")
    if not request_id or len(request_id) > 200:
        raise ValueError("Request ID is required and must be at most 200 characters")

    queue = read_json(queue_path, {"items": []})
    if not isinstance(queue, dict) or not isinstance(queue.get("items"), list):
        raise ValueError(f"Invalid research queue: {queue_path}")
    if not any(
        isinstance(item, dict) and item.get("stable_id") == article_id
        for item in queue["items"]
    ):
        raise ValueError(f"Article is not in the active review queue: {article_id}")
    queue_item = next(item for item in queue["items"] if item["stable_id"] == article_id)

    records = load_decisions(decisions_path)
    if any(
        record["article_id"] == article_id and record["request_id"] == request_id
        for record in records
    ):
        return False
    if (
        not any(record["article_id"] == article_id for record in records)
        and queue_item.get("review_status") in {"approved", "rejected"}
        and isinstance(queue_item.get("reviewed_by"), str)
        and queue_item["reviewed_by"].strip()
        and isinstance(queue_item.get("reviewed_at"), str)
    ):
        try:
            legacy_time = datetime.fromisoformat(
                queue_item["reviewed_at"].replace("Z", "+00:00")
            )
        except ValueError:
            legacy_time = None
        if legacy_time and legacy_time.tzinfo:
            records.append(
                {
                    "article_id": article_id,
                    "decision": (
                        "approve" if queue_item["review_status"] == "approved" else "reject"
                    ),
                    "status": queue_item["review_status"],
                    "timestamp": legacy_time.astimezone(timezone.utc).isoformat(
                        timespec="seconds"
                    ).replace("+00:00", "Z"),
                    "reviewer": queue_item["reviewed_by"].strip(),
                    "notes": "",
                    "request_id": "legacy-import-" + hashlib.sha256(
                        article_id.encode("utf-8")
                    ).hexdigest(),
                }
            )
    status = current_status({"stable_id": article_id}, records)
    if decision == "reconsider" and status == "awaiting_review":
        raise ValueError("Only an article that has already been reviewed or held can be reconsidered")

    when = (timestamp or datetime.now(timezone.utc)).astimezone(timezone.utc)
    records.append(
        {
            "article_id": article_id,
            "decision": decision,
            "status": DECISION_STATUSES[decision],
            "timestamp": when.isoformat(timespec="seconds").replace("+00:00", "Z"),
            "reviewer": reviewer,
            "notes": notes,
            "request_id": request_id,
        }
    )
    decisions_path.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(
        {"schema_version": 1, "decisions": records},
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ) + "\n"
    temporary_path = decisions_path.with_suffix(decisions_path.suffix + ".tmp")
    temporary_path.write_text(serialized, encoding="utf-8", newline="\n")
    os.replace(temporary_path, decisions_path)
    return True


def submit_command(
    args: argparse.Namespace,
    *,
    decisions_path: Path = DEFAULT_DECISIONS,
) -> int:
    created = record_decision(
        args.article_id,
        args.decision,
        args.reviewer,
        args.notes,
        args.request_id,
        queue_path=args.queue,
        decisions_path=decisions_path,
    )
    print("Editorial decision recorded." if created else "Duplicate request ignored.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    submit = subparsers.add_parser("submit", help="append one validated editorial decision")
    submit.add_argument("--article-id", required=True)
    submit.add_argument("--decision", choices=sorted(DECISION_STATUSES), required=True)
    submit.add_argument("--reviewer", required=True)
    submit.add_argument("--notes", default="")
    submit.add_argument("--request-id", required=True)
    submit.add_argument(
        "--queue",
        type=Path,
        default=DEFAULT_QUEUE,
        help="active review queue to validate against",
    )
    args = parser.parse_args()
    try:
        return submit_command(args)
    except (OSError, ValueError) as exc:
        print(f"Editorial decision failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
