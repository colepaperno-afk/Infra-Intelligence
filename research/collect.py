#!/usr/bin/env python3
"""Collect and normalize publicly accessible infrastructure research feeds."""

from __future__ import annotations

import argparse
import email.utils
from html.parser import HTMLParser
import json
import logging
import os
from pathlib import Path
import re
import sys
import time
from datetime import datetime, timezone
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET


LOGGER = logging.getLogger("infra_research")
ROOT = Path(__file__).resolve().parent
DEFAULT_REGISTRY = ROOT / "sources.json"
DEFAULT_OUTPUT = ROOT / "data" / "research-queue.json"
USER_AGENT = "InfraIntelligenceResearchCollector/1.0 (public RSS and Atom feeds)"
REQUEST_TIMEOUT_SECONDS = 20
MAX_FEED_BYTES = 5 * 1024 * 1024
MAX_ITEMS = 500
MAX_EXCERPT_LENGTH = 420
RETRY_DELAYS_SECONDS = (1, 2)
CURRENT_SCHEMA_VERSION = 2
DEFAULT_RULES = ROOT / "relevance-rules.json"

CATEGORY_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "robotics",
        (
            r"\b(?:robot(?:ics)?|humanoid|cobot|industrial automation|industrial AI|robotic arm|physical AI|automation deployment)\b",
        ),
    ),
    (
        "ai_semiconductors",
        (
            r"\bAI chips?\b",
            r"\bsemiconduct(?:or|ors)\b",
            r"\b(?:GPUs?|accelerators?|inference|rack[- ]scale compute|foundr(?:y|ies)|wafers?|HBM|memory)\b",
            r"\badvanced packaging\b",
            r"\bAI compute\b",
        ),
    ),
    (
        "networking",
        (
            r"\b(?:network(?:ing)?|ethernet|optical interconnect|fiber(?:-optic)?|switching|router|interconnect)\b",
        ),
    ),
    (
        "data_centers",
        (
            r"\bdata[ -]?cent(?:er|re)s?\b",
            r"\b(?:colocation|hyperscale|cloud infrastructure|server farm|data hall)\b",
        ),
    ),
    (
        "energy_power",
        (
            r"\b(?:energy|power|electricity|generation|substation|transmission|nuclear|turbine|transformer|utility|solar|wind power|battery storage|grid)\b",
        ),
    ),
    (
        "cooling",
        (
            r"\b(?:cooling|thermal management|heat rejection)\b",
        ),
    ),
)

COMPANY_ALIASES: dict[str, tuple[str, ...]] = {
    "NVDA": ("NVIDIA", "NVDA"),
    "AMD": ("Advanced Micro Devices", "AMD"),
    "EQIX": ("Equinix", "EQIX"),
    "INTC": ("Intel", "INTC"),
    "AVGO": ("Broadcom", "AVGO"),
    "ANET": ("Arista Networks", "ANET"),
    "VRT": ("Vertiv", "VRT"),
    "ETN": ("Eaton", "ETN"),
    "TSLA": ("Tesla", "TSLA"),
    "AMZN": ("Amazon", "AMZN"),
    "GOOGL": ("Alphabet", "Google", "GOOGL"),
    "MSFT": ("Microsoft", "MSFT"),
    "ORCL": ("Oracle", "ORCL"),
    "TSM": ("Taiwan Semiconductor Manufacturing", "TSMC", "TSM"),
    "MU": ("Micron", "Micron Technology", "MU"),
}

OTHER_CATEGORY = "other"

TRACKING_PARAMETERS = {
    "fbclid",
    "gclid",
    "mc_cid",
    "mc_eid",
    "ref",
    "source",
}


class FeedError(Exception):
    """A source response cannot be safely used as a feed."""


class HTMLTextExtractor(HTMLParser):
    """Extract readable text from HTML snippets embedded in feed descriptions."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() in {"script", "style"}:
            self.hidden_depth += 1
        elif tag.lower() in {"p", "br", "div", "li"} and self.hidden_depth == 0:
            self.parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"script", "style"} and self.hidden_depth:
            self.hidden_depth -= 1
        elif tag.lower() in {"p", "div", "li"} and self.hidden_depth == 0:
            self.parts.append(" ")

    def handle_data(self, data: str) -> None:
        if self.hidden_depth == 0:
            self.parts.append(data)


def clean_text(value: str, limit: int | None = None) -> str:
    parser = HTMLTextExtractor()
    parser.feed(value)
    text = " ".join(" ".join(parser.parts).split())
    if limit is not None and len(text) > limit:
        text = text[: limit - 1].rsplit(" ", 1)[0].rstrip() + "…"
    return text


def validated_http_url(value: str) -> str:
    """Validate a source or item URL before storing or requesting it."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("URL is missing")
    value = value.strip()
    try:
        parts = urlsplit(value)
        _ = parts.port
    except ValueError as exc:
        raise ValueError("URL is malformed") from exc
    if parts.scheme.lower() not in {"http", "https"}:
        raise ValueError("URL must use HTTP or HTTPS")
    if not parts.hostname or parts.username or parts.password:
        raise ValueError("URL must include a hostname and must not include credentials")
    if any(char.isspace() or ord(char) < 32 for char in value):
        raise ValueError("URL contains whitespace or control characters")
    if re.search(r"%(?![0-9a-fA-F]{2})", value):
        raise ValueError("URL contains an invalid percent escape")
    return value


def canonicalize_url(value: str) -> str:
    """Normalize tracking variants while preserving meaningful query parameters."""
    value = validated_http_url(value)
    parts = urlsplit(value)
    host = (parts.hostname or "").lower()
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    port = parts.port
    netloc = host
    if port and not (
        (parts.scheme.lower() == "http" and port == 80)
        or (parts.scheme.lower() == "https" and port == 443)
    ):
        netloc = f"{host}:{port}"
    kept_query = [
        (key, val)
        for key, val in parse_qsl(parts.query, keep_blank_values=True)
        if key.lower() not in TRACKING_PARAMETERS and not key.lower().startswith("utm_")
    ]
    path = re.sub(r"/{2,}", "/", parts.path or "/")
    if path != "/":
        path = path.rstrip("/")
    return urlunsplit(
        (
            parts.scheme.lower(),
            netloc,
            path,
            urlencode(sorted(kept_query)),
            "",
        )
    )


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _child_text(node: ET.Element, names: tuple[str, ...]) -> str:
    for child in node.iter():
        if child is node or _local_name(child.tag) not in names:
            continue
        value = " ".join("".join(child.itertext()).split())
        if value:
            return value
    return ""


def parse_timestamp(value: str) -> str | None:
    value = value.strip()
    if not value:
        return None
    parsed: datetime | None = None
    try:
        parsed = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def classify_entry(
    text: str,
    fallback: str | None = None,
    rules: dict[str, Any] | None = None,
) -> str | None:
    config = rules or DEFAULT_RULES_CONFIG
    if _has_match(text, config["patterns"]["gaming"]) and not _has_match(
        text, config["patterns"]["infra_specific"]
    ):
        return None
    if (
        _has_match(text, config["topic_patterns"]["energy_power"])
        and _has_match(text, config["patterns"]["compute_link"])
    ):
        return "energy_power"
    for category, patterns in CATEGORY_RULES:
        if any(re.search(pattern, text, re.IGNORECASE) for pattern in patterns):
            return category
    return None


def _has_match(text: str, patterns: list[str]) -> bool:
    return any(re.search(pattern, text, re.IGNORECASE) for pattern in patterns)


def load_scoring_rules(path: Path = DEFAULT_RULES) -> dict[str, Any]:
    try:
        rules = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Could not load relevance rules {path}: {exc}") from exc
    if not isinstance(rules, dict) or rules.get("version") != 1:
        raise ValueError(f"Unsupported relevance rules in {path}")
    for group in ("patterns", "topic_patterns", "score_weights"):
        if not isinstance(rules.get(group), dict):
            raise ValueError(f"Relevance rules require a valid '{group}' object")
    if not isinstance(rules.get("event_types"), list) or not isinstance(
        rules.get("company_entities"), list
    ):
        raise ValueError("Relevance rules require event_types and company_entities arrays")
    try:
        for group_name in ("patterns", "topic_patterns"):
            for name, patterns in rules[group_name].items():
                if not isinstance(name, str) or not isinstance(patterns, list):
                    raise ValueError(f"{group_name}.{name} must be a string array")
                for pattern in patterns:
                    if not isinstance(pattern, str):
                        raise ValueError(f"{group_name}.{name} contains a non-string pattern")
                    re.compile(pattern, re.IGNORECASE)
        for event in rules["event_types"]:
            if (
                not isinstance(event, dict)
                or not isinstance(event.get("type"), str)
                or not isinstance(event.get("patterns"), list)
            ):
                raise ValueError("Each event type requires a name and pattern array")
            for pattern in event["patterns"]:
                if not isinstance(pattern, str):
                    raise ValueError("Event patterns must be strings")
                re.compile(pattern, re.IGNORECASE)
        for entity in rules["company_entities"]:
            if (
                not isinstance(entity, dict)
                or not isinstance(entity.get("name"), str)
                or not isinstance(entity.get("aliases"), list)
                or any(not isinstance(alias, str) for alias in entity["aliases"])
            ):
                raise ValueError("Each company entity requires a name and string aliases")
    except (TypeError, KeyError, re.error) as exc:
        raise ValueError(f"Invalid pattern in relevance rules {path}: {exc}") from exc
    if (
        not isinstance(rules.get("maximum_score"), int)
        or not 1 <= rules["maximum_score"] <= 100
        or not isinstance(rules.get("stale_after_days"), int)
        or rules["stale_after_days"] < 1
    ):
        raise ValueError("Relevance rules contain invalid maximum_score or stale_after_days")
    weights = rules["score_weights"]
    if (
        not isinstance(weights.get("topic"), dict)
        or any(
            not isinstance(value, int) or value < 0
            for value in weights["topic"].values()
        )
        or any(
            not isinstance(weights.get(key), int) or weights[key] < 0
            for key in ("compute_link", "major_event", "measurable_detail", "public_company")
        )
    ):
        raise ValueError("Relevance rules contain invalid score weights")
    return rules


DEFAULT_RULES_CONFIG = load_scoring_rules()


def identify_companies(
    text: str,
    source_tickers: list[str] | None = None,
    rules: dict[str, Any] | None = None,
) -> tuple[list[str], list[str]]:
    config = rules or DEFAULT_RULES_CONFIG
    names: set[str] = set()
    tickers: set[str] = set(source_tickers or [])
    for entity in config["company_entities"]:
        if entity.get("ticker") in tickers:
            names.add(entity["name"])
        for alias in entity["aliases"]:
            if alias.isupper() and len(alias) <= 5:
                match = re.search(
                    rf"(?<!\w)(?:\${re.escape(alias)}|{re.escape(alias)})(?!\w)",
                    text,
                )
            else:
                match = re.search(rf"(?<!\w){re.escape(alias)}(?!\w)", text, re.IGNORECASE)
            if match:
                names.add(entity["name"])
                if entity.get("ticker"):
                    tickers.add(entity["ticker"])
                break
    return sorted(names), sorted(tickers)


def identify_tickers(text: str, source_tickers: list[str] | None = None) -> list[str]:
    return identify_companies(text, source_tickers)[1]


def classify_event(text: str, rules: dict[str, Any] | None = None) -> str:
    config = rules or DEFAULT_RULES_CONFIG
    for event in config["event_types"]:
        if _has_match(text, event["patterns"]):
            return event["type"]
    return "other"


def score_article(
    title: str,
    excerpt: str,
    published_at: str,
    retrieved_at: str,
    category: str,
    mentioned_companies: list[str],
    rules: dict[str, Any] | None = None,
    source_url: str | None = None,
) -> dict[str, Any]:
    config = rules or DEFAULT_RULES_CONFIG
    text = f"{title} {excerpt}"
    weights = config["score_weights"]
    score = 0
    reasons: list[str] = []
    topic_patterns = config["topic_patterns"].get(category, [])
    topic_match = bool(topic_patterns) and _has_match(text, topic_patterns)
    compute_match = _has_match(text, config["patterns"]["compute_link"])
    consumer_gaming_without_infrastructure = (
        _has_match(text, config["patterns"]["gaming"])
        and not topic_match
        and not compute_match
    )
    opinion = _has_match(title, config["patterns"]["opinion"]) or bool(
        source_url
        and re.search(r"/opinions?/", urlsplit(source_url).path, re.IGNORECASE)
    )
    sponsored = _has_match(text, config["patterns"]["sponsorship"])
    evergreen = _has_match(text, config["patterns"]["evergreen"])
    exclusion_reason: str | None = None

    try:
        age_days = (
            datetime.fromisoformat(retrieved_at.replace("Z", "+00:00"))
            - datetime.fromisoformat(published_at.replace("Z", "+00:00"))
        ).total_seconds() / 86400
    except (TypeError, ValueError):
        age_days = 0

    if age_days > config["stale_after_days"]:
        exclusion_reason = f"Published more than {config['stale_after_days']} days before collection."
    elif opinion:
        exclusion_reason = "Opinion or commentary content is not eligible for active review."
    elif sponsored:
        exclusion_reason = "Marked as sponsored or promotional content."
    elif consumer_gaming_without_infrastructure:
        exclusion_reason = "Consumer gaming coverage without a clear AI infrastructure connection."
    elif category == "energy_power" and not compute_match:
        exclusion_reason = "Energy coverage lacks a meaningful link to AI compute infrastructure."
    elif not topic_match:
        exclusion_reason = "No covered infrastructure topic matched the article text."
    else:
        topic_points = weights["topic"].get(category, 0)
        score += topic_points
        reasons.append(f"Relevant {category.replace('_', ' ')} topic (+{topic_points}).")
        if compute_match:
            points = weights["compute_link"]
            score += points
            reasons.append(f"Explicit compute-infrastructure connection (+{points}).")
        if classify_event(text, config) != "other":
            points = weights["major_event"]
            score += points
            reasons.append(f"Concrete development: {classify_event(text, config).replace('_', ' ')} (+{points}).")
        if _has_match(text, config["patterns"]["measurable_detail"]):
            points = weights["measurable_detail"]
            score += points
            reasons.append(f"Quantified investment, capacity, or scale detail (+{points}).")
        if mentioned_companies:
            points = weights["public_company"]
            score += points
            reasons.append(f"Identifies a publicly traded or named infrastructure company (+{points}).")

    if exclusion_reason:
        score = 0
        reasons.append(exclusion_reason)
    if evergreen and not exclusion_reason and topic_match:
        cap = 59
        score = max(40, min(score, cap))
        reasons.append("Evergreen or explanatory coverage capped at low priority.")
    score = min(max(score, 0), config["maximum_score"])
    if score < 40 and not exclusion_reason:
        exclusion_reason = "Relevance score is below the active-review threshold."
    if score < 40 and exclusion_reason and exclusion_reason not in reasons:
        reasons.append(exclusion_reason)
    priority = (
        "high" if score >= 80 else
        "medium" if score >= 60 else
        "low" if score >= 40 else
        "excluded"
    )
    return {
        "relevance_score": score,
        "priority": priority,
        "relevance_reasons": reasons,
        "event_type": classify_event(text, config),
        "exclusion_reason": exclusion_reason if priority == "excluded" else None,
    }


def _entries(root: ET.Element) -> list[ET.Element]:
    root_name = _local_name(root.tag)
    if root_name == "rss":
        return [child for child in root.iter() if _local_name(child.tag) == "item"]
    if root_name == "feed":
        return [child for child in root if _local_name(child.tag) == "entry"]
    if root_name == "rdf":
        return [child for child in root if _local_name(child.tag) == "item"]
    raise FeedError(f"Unsupported feed root element: {root_name}")


def _entry_link(entry: ET.Element) -> str:
    for child in entry:
        if _local_name(child.tag) != "link":
            continue
        href = child.attrib.get("href")
        if href:
            rel = child.attrib.get("rel", "alternate").lower()
            if rel in {"alternate", ""}:
                return href
        if child.text and child.text.strip():
            return child.text.strip()
    return ""


def parse_feed(
    payload: bytes,
    source: dict[str, Any],
    retrieved_at: str,
    rules: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    config = rules or DEFAULT_RULES_CONFIG
    try:
        root = ET.fromstring(payload)
        entries = _entries(root)
    except ET.ParseError as exc:
        raise FeedError(f"Invalid XML: {exc}") from exc

    normalized: list[dict[str, Any]] = []
    for position, entry in enumerate(entries, start=1):
        title = clean_text(_child_text(entry, ("title",)))
        raw_url = _entry_link(entry)
        try:
            source_url = validated_http_url(raw_url)
            canonical_url = canonicalize_url(source_url)
        except ValueError as exc:
            LOGGER.warning("%s entry %d rejected: invalid URL (%s)", source["id"], position, exc)
            continue

        published_raw = _child_text(
            entry, ("pubdate", "published", "updated", "date", "created")
        )
        published_at = parse_timestamp(published_raw)
        if not published_at:
            LOGGER.warning("%s entry %d rejected: missing or invalid publication timestamp", source["id"], position)
            continue
        if not title:
            LOGGER.warning("%s entry %d rejected: missing title", source["id"], position)
            continue

        description = _child_text(
            entry,
            ("encoded", "description", "summary", "content", "subtitle"),
        )
        excerpt = clean_text(description, MAX_EXCERPT_LENGTH) or title
        category = classify_entry(f"{title} {excerpt}", rules=config) or OTHER_CATEGORY
        mentioned_companies, tickers = identify_companies(
            f"{title} {excerpt}", source.get("company_tickers"), config
        )
        relevance = score_article(
            title,
            excerpt,
            published_at,
            retrieved_at,
            category,
            mentioned_companies,
            config,
            source_url=source_url,
        )

        stable_identifier = _child_text(entry, ("guid", "id"))
        if not stable_identifier:
            stable_identifier = canonical_url
        stable_id = f"{source['id']}:{stable_identifier}"
        normalized.append(
            {
                "stable_id": stable_id,
                "canonical_url": canonical_url,
                "title": title,
                "publisher": source["publisher"],
                "source_id": source["id"],
                "source_url": source_url,
                "feed_url": source["url"],
                "published_at": published_at,
                "retrieved_at": retrieved_at,
                "category": category,
                "affected_tickers": tickers,
                "mentioned_companies": mentioned_companies,
                "excerpt": excerpt,
                "review_status": "unreviewed",
                **relevance,
            }
        )
    return normalized


def validate_source_registry(raw_sources: Any) -> list[dict[str, Any]]:
    if not isinstance(raw_sources, list) or not raw_sources:
        raise ValueError("Source registry must be a non-empty JSON array")
    sources: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for number, source in enumerate(raw_sources, start=1):
        if not isinstance(source, dict):
            raise ValueError(f"Source {number} must be a JSON object")
        for field in ("id", "publisher", "url", "source_type"):
            if not isinstance(source.get(field), str) or not source[field].strip():
                raise ValueError(f"Source {number} is missing required field '{field}'")
        source = dict(source)
        source["url"] = validated_http_url(source["url"])
        if source["id"] in seen_ids:
            raise ValueError(f"Duplicate source id: {source['id']}")
        seen_ids.add(source["id"])
        company_tickers = source.get("company_tickers", [])
        if not isinstance(company_tickers, list) or any(
            not isinstance(ticker, str) or ticker not in COMPANY_ALIASES
            for ticker in company_tickers
        ):
            raise ValueError(f"Invalid company_tickers for {source['id']}")
        sources.append(source)
    return sources


def load_sources(path: Path = DEFAULT_REGISTRY) -> list[dict[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Could not load source registry {path}: {exc}") from exc
    return validate_source_registry(payload)


def fetch_feed(source: dict[str, Any]) -> bytes:
    url = validated_http_url(source["url"])
    request = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml"})
    for attempt in range(len(RETRY_DELAYS_SECONDS) + 1):
        try:
            with urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                payload = response.read(MAX_FEED_BYTES + 1)
            if len(payload) > MAX_FEED_BYTES:
                raise FeedError(f"Feed exceeds the {MAX_FEED_BYTES}-byte size limit")
            return payload
        except HTTPError as exc:
            if exc.code not in {408, 425, 429} and exc.code < 500:
                raise FeedError(f"HTTP {exc.code}") from exc
            failure: Exception = exc
        except (URLError, TimeoutError, OSError) as exc:
            failure = exc
        if attempt >= len(RETRY_DELAYS_SECONDS):
            raise FeedError(f"Request failed after {attempt + 1} attempts: {failure}") from failure
        delay = RETRY_DELAYS_SECONDS[attempt]
        LOGGER.warning(
            "%s fetch attempt %d failed (%s); retrying in %d second(s)",
            source["id"],
            attempt + 1,
            failure,
            delay,
        )
        time.sleep(delay)
    raise AssertionError("unreachable")


def load_queue_document(
    path: Path,
    rules: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if not path.exists():
        return [], []
    config = rules or DEFAULT_RULES_CONFIG
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Could not read existing research queue {path}: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") not in {1, CURRENT_SCHEMA_VERSION}:
        raise ValueError(f"Unsupported research queue schema in {path}")
    items = payload.get("items")
    if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
        raise ValueError(f"Invalid items array in research queue {path}")
    excluded_items = payload.get("excluded_items", [])
    if not isinstance(excluded_items, list) or any(
        not isinstance(item, dict) for item in excluded_items
    ):
        raise ValueError(f"Invalid excluded_items array in research queue {path}")
    required_fields = (
        "stable_id",
        "canonical_url",
        "title",
        "publisher",
        "source_id",
        "source_url",
        "feed_url",
        "published_at",
        "retrieved_at",
        "category",
        "affected_tickers",
        "excerpt",
        "review_status",
    )
    valid_categories = {category for category, _ in CATEGORY_RULES} | {OTHER_CATEGORY}
    for number, item in enumerate([*items, *excluded_items], start=1):
        is_active = number <= len(items)
        for field in required_fields:
            if field not in item:
                raise ValueError(f"Queue item {number} is missing required field '{field}'")
        for field in (
            "stable_id",
            "title",
            "publisher",
            "source_id",
            "excerpt",
        ):
            if not isinstance(item[field], str) or not item[field].strip():
                raise ValueError(f"Queue item {number} has an invalid '{field}'")
        for field in ("source_url", "feed_url", "canonical_url"):
            try:
                normalized_url = canonicalize_url(item[field])
            except (TypeError, ValueError) as exc:
                raise ValueError(f"Queue item {number} has an invalid '{field}'") from exc
            if field == "canonical_url" and normalized_url != item[field]:
                raise ValueError(f"Queue item {number} has a non-canonical URL")
        for field in ("published_at", "retrieved_at"):
            if not isinstance(item[field], str) or parse_timestamp(item[field]) is None:
                raise ValueError(f"Queue item {number} has an invalid '{field}'")
        if not isinstance(item["category"], str) or item["category"] not in valid_categories:
            raise ValueError(f"Queue item {number} has an unknown category")
        if not isinstance(item["review_status"], str) or item[
            "review_status"
        ] not in {"unreviewed", "approved", "rejected"}:
            raise ValueError(f"Queue item {number} has an invalid review status")
        tickers = item["affected_tickers"]
        if not isinstance(tickers, list) or any(
            not isinstance(ticker, str) or ticker not in COMPANY_ALIASES
            for ticker in tickers
        ):
            raise ValueError(f"Queue item {number} has invalid affected_tickers")
        if item["review_status"] != "unreviewed":
            if not isinstance(item.get("reviewed_by"), str) or not item["reviewed_by"].strip():
                raise ValueError(f"Reviewed queue item {number} requires reviewed_by")
            if not isinstance(item.get("reviewed_at"), str) or parse_timestamp(
                item["reviewed_at"]
            ) is None:
                raise ValueError(f"Reviewed queue item {number} requires a valid reviewed_at")
        if payload.get("schema_version") == CURRENT_SCHEMA_VERSION:
            if (
                not isinstance(item.get("relevance_score"), int)
                or not 0 <= item["relevance_score"] <= 100
                or item.get("priority")
                not in {"high", "medium", "low", "excluded"}
                or not isinstance(item.get("relevance_reasons"), list)
                or any(not isinstance(reason, str) for reason in item["relevance_reasons"])
                or not isinstance(item.get("event_type"), str)
                or not isinstance(item.get("mentioned_companies"), list)
                or any(
                    not isinstance(company, str)
                    for company in item["mentioned_companies"]
                )
                or item["priority"]
                != (
                    "high"
                    if item["relevance_score"] >= 80
                    else "medium"
                    if item["relevance_score"] >= 60
                    else "low"
                    if item["relevance_score"] >= 40
                    else "excluded"
                )
                or is_active != (item["relevance_score"] >= 40)
            ):
                raise ValueError(f"Queue item {number} has invalid relevance metadata")
            exclusion_reason = item.get("exclusion_reason")
            if item["relevance_score"] < 40 and (
                not isinstance(exclusion_reason, str) or not exclusion_reason.strip()
            ):
                raise ValueError(f"Excluded queue item {number} requires exclusion_reason")
        else:
            companies, _ = identify_companies(
                f"{item['title']} {item['excerpt']}",
                item["affected_tickers"],
                config,
            )
            item["mentioned_companies"] = companies
            item.update(
                score_article(
                    item["title"],
                    item["excerpt"],
                    item["published_at"],
                    item["retrieved_at"],
                    item["category"],
                    item["mentioned_companies"],
                    config,
                    source_url=item["source_url"],
                )
            )
    return items, excluded_items


def load_queue(path: Path) -> list[dict[str, Any]]:
    return load_queue_document(path)[0]


def deduplicate_items(
    existing: list[dict[str, Any]],
    incoming: list[dict[str, Any]],
    retention_limit: int = MAX_ITEMS,
) -> list[dict[str, Any]]:
    if retention_limit < 1:
        raise ValueError("Retention limit must be positive")
    selected: list[dict[str, Any]] = []
    positions: dict[str, int] = {}
    for item in [*existing, *incoming]:
        stable_id = item.get("stable_id")
        canonical_url = item.get("canonical_url")
        if not isinstance(stable_id, str) or not stable_id:
            continue
        try:
            canonical_url = canonicalize_url(canonical_url)
        except (TypeError, ValueError):
            continue
        identity = f"id:{stable_id}"
        existing_position = positions.get(identity)
        if existing_position is None:
            existing_position = positions.get(f"url:{canonical_url}")
        if existing_position is not None:
            previous = selected[existing_position]
            merged = {**previous, **item, "canonical_url": canonical_url}
            for field in (
                "stable_id",
                "canonical_url",
                "source_url",
                "feed_url",
                "published_at",
            ):
                if field in previous:
                    merged[field] = previous[field]
            merged["retrieved_at"] = previous.get(
                "retrieved_at", item.get("retrieved_at")
            )
            if previous.get("review_status") != "unreviewed":
                merged["review_status"] = previous["review_status"]
                for field in ("reviewed_by", "reviewed_at"):
                    if field in previous:
                        merged[field] = previous[field]
            selected[existing_position] = merged
            continue
        selected.append({**item, "canonical_url": canonical_url})
        positions[identity] = len(selected) - 1
        positions[f"url:{canonical_url}"] = len(selected) - 1
    selected.sort(
        key=lambda item: (
            item.get("relevance_score", 0),
            item.get("published_at", ""),
            item.get("canonical_url", ""),
            item.get("stable_id", ""),
        ),
        reverse=True,
    )
    return selected[:retention_limit]


def collect(
    registry_path: Path = DEFAULT_REGISTRY,
    output_path: Path = DEFAULT_OUTPUT,
    now: datetime | None = None,
    rules_path: Path = DEFAULT_RULES,
) -> bool:
    sources = load_sources(registry_path)
    rules = load_scoring_rules(rules_path)
    existing, existing_excluded = load_queue_document(output_path, rules)
    retrieved_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    retrieved_at_text = retrieved_at.isoformat(timespec="seconds").replace("+00:00", "Z")
    incoming: list[dict[str, Any]] = []
    successful_sources = 0
    failed_sources = 0

    for source in sources:
        LOGGER.info("Fetching %s (%s)", source["publisher"], source["url"])
        try:
            payload = fetch_feed(source)
            findings = parse_feed(payload, source, retrieved_at_text, rules)
        except (FeedError, ValueError) as exc:
            failed_sources += 1
            LOGGER.error("%s collection failed: %s", source["id"], exc)
            continue
        successful_sources += 1
        incoming.extend(findings)
        LOGGER.info("%s yielded %d feed item(s)", source["id"], len(findings))

    if successful_sources == 0:
        raise FeedError("All configured sources failed; existing queue was left unchanged")

    merged = deduplicate_items([*existing, *existing_excluded], incoming)
    active = [item for item in merged if item["relevance_score"] >= 40]
    excluded = [item for item in merged if item["relevance_score"] < 40]
    active = deduplicate_items([], active)
    excluded = deduplicate_items([], excluded)
    document = {
        "schema_version": CURRENT_SCHEMA_VERSION,
        "items": active,
        "excluded_items": excluded,
    }
    serialized = json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    previous = output_path.read_text(encoding="utf-8") if output_path.exists() else None
    if serialized == previous:
        LOGGER.info("Research queue unchanged; no file written")
        changed = False
    else:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = output_path.with_suffix(output_path.suffix + ".tmp")
        temporary_path.write_text(serialized, encoding="utf-8", newline="\n")
        os.replace(temporary_path, output_path)
        changed = True
        LOGGER.info(
            "Updated research queue: %d active item(s), %d excluded record(s)",
            len(active),
            len(excluded),
        )

    LOGGER.info(
        "Collection summary: %d source(s) succeeded, %d failed, %d feed item(s), %d active",
        successful_sources,
        failed_sources,
        len(incoming),
        len(active),
    )
    LOGGER.info("%d excluded item(s) retained for audit", len(excluded))
    if failed_sources:
        LOGGER.warning("Some sources failed; review logs before approving the queue pull request")
    return changed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--rules", type=Path, default=DEFAULT_RULES)
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
    )
    try:
        collect(args.registry, args.output, rules_path=args.rules)
    except (FeedError, ValueError, OSError) as exc:
        LOGGER.error("Collection aborted: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
