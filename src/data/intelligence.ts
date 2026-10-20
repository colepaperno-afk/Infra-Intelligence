import generated from "./approved-research.generated.json";

export interface PublicArticle {
  stableId: string;
  slug: string;
  title: string;
  publisher: string;
  sourceId: string;
  sourceUrl: string;
  feedUrl: string | null;
  publishedAt: string | null;
  retrievedAt: string | null;
  category: string;
  relevanceScore: number | null;
  eventType: string;
  mentionedCompanies: string[];
  tickerCandidates: string[];
  verifiedTickers: string[];
  excerpt: string;
}

export interface IntelligenceCompany {
  slug: string;
  name: string;
  articles: PublicArticle[];
}

export const approvedArticles: PublicArticle[] = generated.articles;

export function companySlug(name: string): string {
  const readable = name
    .toLowerCase()
    .normalize("NFKD")
    .replace(/[\u0300-\u036f]/g, "")
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-|-$/g, "") || "company";
  const hash = [...name].reduce(
    (value, character) => (value * 31 + character.charCodeAt(0)) >>> 0,
    7,
  );
  return `${readable}-${hash.toString(36)}`;
}

export const intelligenceCompanies: IntelligenceCompany[] = (() => {
  const byName = new Map<string, PublicArticle[]>();
  for (const article of approvedArticles) {
    for (const name of article.mentionedCompanies) {
      const stories = byName.get(name) ?? [];
      if (!stories.some((story) => story.stableId === article.stableId)) {
        stories.push(article);
      }
      byName.set(name, stories);
    }
  }
  return [...byName.entries()]
    .map(([name, articles]) => ({
      name,
      slug: companySlug(name),
      articles: [...articles].sort(
        (left, right) => (right.publishedAt ?? "").localeCompare(left.publishedAt ?? ""),
      ),
    }))
    .sort((left, right) => left.name.localeCompare(right.name));
})();

export function formatCategory(value: string): string {
  return value.replaceAll("_", " ").replace(/\b\w/g, (character) => character.toUpperCase());
}

export function formatDate(value: string | null): string {
  if (!value) return "Date unavailable";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "Date unavailable";
  return new Intl.DateTimeFormat("en-US", {
    dateStyle: "medium",
    timeStyle: "short",
    timeZone: "UTC",
  }).format(date) + " UTC";
}
