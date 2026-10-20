import { createHash } from "node:crypto";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const RESEARCH_DIR = dirname(fileURLToPath(import.meta.url));
const REPOSITORY_ROOT = resolve(RESEARCH_DIR, "..");
const DEFAULT_QUEUE = resolve(RESEARCH_DIR, "data/research-queue.json");
const DEFAULT_DECISIONS = resolve(RESEARCH_DIR, "data/editorial-decisions.json");
const DEFAULT_SOURCES = resolve(RESEARCH_DIR, "sources.json");
const DEFAULT_OUTPUT = resolve(
  REPOSITORY_ROOT,
  "src/data/approved-research.generated.json",
);
const DECISION_STATUSES = {
  approve: "approved",
  reject: "rejected",
  hold: "on_hold",
  reconsider: "awaiting_review",
};

function readJson(path) {
  try {
    return JSON.parse(readFileSync(path, "utf8"));
  } catch (error) {
    throw new Error(`Could not read valid JSON from ${path}: ${error.message}`, {
      cause: error,
    });
  }
}

function isRecord(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function validateUrl(value) {
  if (typeof value !== "string" || !value.trim()) return null;
  try {
    const url = new URL(value);
    if (
      !["http:", "https:"].includes(url.protocol)
      || !url.hostname
      || url.username
      || url.password
    ) {
      return null;
    }
    return url;
  } catch {
    return null;
  }
}

function canonicalUrl(value) {
  const url = validateUrl(value);
  if (!url) return null;
  url.hash = "";
  for (const key of [...url.searchParams.keys()]) {
    if (key.toLowerCase().startsWith("utm_") || [
      "fbclid",
      "gclid",
      "mc_cid",
      "mc_eid",
      "ref",
      "source",
    ].includes(key.toLowerCase())) {
      url.searchParams.delete(key);
    }
  }
  url.searchParams.sort();
  if (url.pathname !== "/") url.pathname = url.pathname.replace(/\/+$/, "");
  return url.href;
}

function articleSlug(stableId) {
  const readable = stableId
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-|-$/g, "")
    .slice(0, 56) || "article";
  const suffix = createHash("sha256").update(stableId).digest("hex").slice(0, 8);
  return `${readable}-${suffix}`;
}

function textList(value) {
  return Array.isArray(value)
    ? [...new Set(value.filter((item) => typeof item === "string" && item.trim()))]
    : [];
}

function validateInputs(queue, decisionStore, sources) {
  if (!isRecord(queue) || !Array.isArray(queue.items)) {
    throw new Error("Research queue must contain an items array");
  }
  for (const [index, item] of queue.items.entries()) {
    if (!isRecord(item) || typeof item.stable_id !== "string" || !item.stable_id.trim()) {
      throw new Error(`Research queue item ${index + 1} requires a stable_id`);
    }
  }
  if (
    !isRecord(decisionStore)
    || decisionStore.schema_version !== 1
    || !Array.isArray(decisionStore.decisions)
  ) {
    throw new Error("Editorial decision store must use schema version 1 and contain decisions");
  }
  if (!Array.isArray(sources)) {
    throw new Error("Source registry must be an array");
  }
  const seenRequests = new Set();
  for (const [index, record] of decisionStore.decisions.entries()) {
    if (
      !isRecord(record)
      || typeof record.article_id !== "string"
      || !record.article_id.trim()
      || record.article_id.length > 1000
      || !Object.hasOwn(DECISION_STATUSES, record.decision)
      || record.status !== DECISION_STATUSES[record.decision]
      || typeof record.timestamp !== "string"
      || Number.isNaN(Date.parse(record.timestamp))
      || !/(?:Z|[+-]\d{2}:\d{2})$/i.test(record.timestamp)
      || typeof record.reviewer !== "string"
      || !record.reviewer.trim()
      || record.reviewer.length > 120
      || typeof record.request_id !== "string"
      || !record.request_id.trim()
      || record.request_id.length > 200
      || (record.notes !== undefined
        && (typeof record.notes !== "string" || record.notes.length > 2000))
    ) {
      throw new Error(`Editorial decision ${index + 1} is invalid`);
    }
    const requestKey = `${record.article_id}\u0000${record.request_id}`;
    if (seenRequests.has(requestKey)) {
      throw new Error(`Editorial decision ${index + 1} repeats a request ID`);
    }
    seenRequests.add(requestKey);
  }
}

export function buildApprovedContent(queue, decisionStore, sources) {
  validateInputs(queue, decisionStore, sources);

  const latestDecision = new Map();
  for (const decision of decisionStore.decisions) {
    latestDecision.set(decision.article_id, decision);
  }

  const sourcesById = new Map(
    sources
      .filter((source) => isRecord(source) && typeof source.id === "string")
      .map((source) => [source.id, source]),
  );
  const seenIds = new Set();
  const seenUrls = new Set();
  const candidates = queue.items
    .filter((item) => isRecord(item) && typeof item.stable_id === "string")
    .filter((item) => {
      const decision = latestDecision.get(item.stable_id);
      return decision?.decision === "approve" && decision.status === "approved";
    })
    .sort((left, right) => (
      String(right.published_at ?? "").localeCompare(String(left.published_at ?? ""))
      || (Number(right.relevance_score) || 0) - (Number(left.relevance_score) || 0)
      || left.stable_id.localeCompare(right.stable_id)
    ));

  const articles = [];
  for (const item of candidates) {
    if (seenIds.has(item.stable_id)) continue;
    seenIds.add(item.stable_id);

    const sourceUrl = item.source_url ?? item.canonical_url;
    const canonical = canonicalUrl(sourceUrl);
    if (!canonical) {
      console.warn(`Skipping approved article ${item.stable_id}: no valid HTTP source URL`);
      continue;
    }
    if (seenUrls.has(canonical)) continue;
    seenUrls.add(canonical);

    const source = sourcesById.get(item.source_id);
    const verifiedTickers = textList(source?.company_tickers);
    const tickerCandidates = textList(item.affected_tickers)
      .filter((ticker) => !verifiedTickers.includes(ticker));
    const publishedAt = typeof item.published_at === "string"
      && !Number.isNaN(Date.parse(item.published_at))
      ? item.published_at
      : null;
    const score = Number.isFinite(item.relevance_score)
      && item.relevance_score >= 0
      && item.relevance_score <= 100
      ? item.relevance_score
      : null;

    articles.push({
      stableId: item.stable_id,
      slug: articleSlug(item.stable_id),
      title: typeof item.title === "string" && item.title.trim()
        ? item.title
        : "Title unavailable",
      publisher: typeof item.publisher === "string" && item.publisher.trim()
        ? item.publisher
        : "Publisher unavailable",
      sourceId: typeof item.source_id === "string" ? item.source_id : "",
      sourceUrl: validateUrl(sourceUrl).href,
      feedUrl: validateUrl(item.feed_url)?.href ?? null,
      publishedAt,
      retrievedAt: typeof item.retrieved_at === "string" ? item.retrieved_at : null,
      category: typeof item.category === "string" && item.category.trim()
        ? item.category
        : "other",
      relevanceScore: score,
      eventType: typeof item.event_type === "string" && item.event_type.trim()
        ? item.event_type
        : "other",
      mentionedCompanies: textList(item.mentioned_companies),
      tickerCandidates,
      verifiedTickers,
      excerpt: typeof item.excerpt === "string" ? item.excerpt : "",
    });
  }

  return { schemaVersion: 1, articles };
}

export function exportApprovedContent({
  queuePath = DEFAULT_QUEUE,
  decisionsPath = DEFAULT_DECISIONS,
  sourcesPath = DEFAULT_SOURCES,
  outputPath = DEFAULT_OUTPUT,
} = {}) {
  const content = buildApprovedContent(
    readJson(queuePath),
    readJson(decisionsPath),
    readJson(sourcesPath),
  );
  mkdirSync(dirname(outputPath), { recursive: true });
  writeFileSync(outputPath, `${JSON.stringify(content, null, 2)}\n`, "utf8");
  console.info(`Exported ${content.articles.length} human-approved articles`);
  return content;
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  exportApprovedContent();
}
