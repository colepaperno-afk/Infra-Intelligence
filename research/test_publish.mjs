import assert from "node:assert/strict";
import test from "node:test";
import { buildApprovedContent } from "./export-approved-content.mjs";

const sources = [
  { id: "official", company_tickers: ["AMD"] },
];
const article = (stableId, overrides = {}) => ({
  stable_id: stableId,
  title: "AI infrastructure expansion",
  publisher: "Research publisher",
  source_id: "official",
  source_url: `https://example.com/${stableId}`,
  feed_url: "https://example.com/feed.xml",
  published_at: "2026-07-22T13:00:00Z",
  retrieved_at: "2026-07-22T14:00:00Z",
  category: "ai_semiconductors",
  relevance_score: 90,
  event_type: "capacity_or_construction",
  mentioned_companies: ["Advanced Micro Devices"],
  affected_tickers: ["AMD", "NVDA"],
  excerpt: "Approved source excerpt.",
  review_status: "unreviewed",
  decision_history: [{ reviewer: "private reviewer", notes: "private note" }],
  ...overrides,
});
const decision = (articleId, value, status) => ({
  article_id: articleId,
  decision: value,
  status,
  reviewer: "private reviewer",
  notes: "private review note",
  timestamp: "2026-07-22T15:00:00Z",
  request_id: `request-${articleId}-${value}`,
});

test("publishes only the latest explicit approval and omits private review fields", () => {
  const content = buildApprovedContent(
    {
      items: [
        article("approved"),
        article("pending", { review_status: "approved" }),
        article("rejected", { review_status: "approved" }),
        article("held"),
        article("legacy", { review_status: "approved" }),
      ],
      excluded_items: [article("excluded")],
    },
    {
      schema_version: 1,
      decisions: [
        decision("approved", "reject", "rejected"),
        decision("approved", "approve", "approved"),
        decision("rejected", "approve", "approved"),
        decision("rejected", "reject", "rejected"),
        decision("held", "hold", "on_hold"),
        decision("excluded", "approve", "approved"),
      ],
    },
    sources,
  );

  assert.deepEqual(content.articles.map((item) => item.stableId), ["approved"]);
  assert.equal(content.articles[0].verifiedTickers.includes("AMD"), true);
  assert.deepEqual(content.articles[0].tickerCandidates, ["NVDA"]);
  assert.equal(JSON.stringify(content).includes("private"), false);
  assert.equal(JSON.stringify(content).includes("decision_history"), false);
});

test("deduplicates stable IDs and canonical source URLs deterministically", () => {
  const content = buildApprovedContent(
    {
      items: [
        article("first", { source_url: "https://example.com/story?utm_source=feed" }),
        article("second", { source_url: "https://example.com/story" }),
        article("first", { title: "Duplicate queue row" }),
      ],
    },
    {
      schema_version: 1,
      decisions: [
        decision("first", "approve", "approved"),
        decision("second", "approve", "approved"),
      ],
    },
    sources,
  );

  assert.equal(content.articles.length, 1);
  assert.equal(content.articles[0].stableId, "first");
});

test("gracefully represents missing optional article metadata", () => {
  const content = buildApprovedContent(
    { items: [article("sparse", {
      title: "",
      publisher: "",
      published_at: "not-a-date",
      category: "",
      relevance_score: "unknown",
      event_type: "",
      mentioned_companies: null,
      affected_tickers: null,
      excerpt: undefined,
      feed_url: "javascript:alert(1)",
    })] },
    {
      schema_version: 1,
      decisions: [decision("sparse", "approve", "approved")],
    },
    sources,
  );

  assert.equal(content.articles.length, 1);
  assert.equal(content.articles[0].title, "Title unavailable");
  assert.equal(content.articles[0].publisher, "Publisher unavailable");
  assert.equal(content.articles[0].publishedAt, null);
  assert.equal(content.articles[0].category, "other");
  assert.equal(content.articles[0].relevanceScore, null);
  assert.equal(content.articles[0].eventType, "other");
  assert.deepEqual(content.articles[0].mentionedCompanies, []);
  assert.equal(content.articles[0].feedUrl, null);
  assert.equal(content.articles[0].excerpt, "");
});

test("does not publish approved records without a valid source link", () => {
  const warnings = [];
  const originalWarn = console.warn;
  console.warn = (message) => warnings.push(message);
  try {
    const content = buildApprovedContent(
      { items: [article("untraceable", { source_url: "javascript:alert(1)" })] },
      {
        schema_version: 1,
        decisions: [decision("untraceable", "approve", "approved")],
      },
      sources,
    );
    assert.deepEqual(content.articles, []);
    assert.match(warnings[0], /no valid HTTP source URL/);
  } finally {
    console.warn = originalWarn;
  }
});

test("rejects malformed decision stores rather than publishing ambiguously", () => {
  assert.throws(
    () => buildApprovedContent(
      { items: [article("ambiguous")] },
      {
        schema_version: 1,
        decisions: [decision("ambiguous", "approve", "rejected")],
      },
      sources,
    ),
    /Editorial decision 1 is invalid/,
  );
});
