# Infra Intelligence

An Astro + TypeScript research interface concept for the infrastructure economy: semiconductors, robotics, data centers, networking, and energy.

All companies, identifiers, signals, dates, chart paths, and network nodes in this prototype are clearly labeled illustrative sample data. The site does not consume live financial or infrastructure data. Newsletter sign-up is a local-only demo and does not send or store email addresses.

## Run locally

```sh
npm install
npm run dev
```

Astro prints the local preview URL (normally `http://localhost:4321`).

## Build

```sh
npm run build
npm run preview
```

The static production output is written to `dist/`.

## Cloudflare Pages

The project is configured for a static Cloudflare Pages build. Use `npm run build` as the build command and `dist` as the build output directory. `wrangler.toml` declares the Pages output directory. No deployment, paid service, or external API is configured.

## Phase 2–3: research collection and prioritization

The standard-library Python collector reads publicly accessible RSS/Atom feeds and writes an explicitly **unreviewed** research queue to `research/data/research-queue.json`. Deterministic relevance scoring prioritizes consequential infrastructure developments, while excluded items remain available in the queue's audit archive. It does not run AI analysis, make investment recommendations, or publish queue entries on the website. Existing illustrative data and labels remain in place.

### Curated sources

The checked-in registry is `research/sources.json`. It records each feed URL, publisher, and source type; a source may also provide a confirmed company ticker. Source metadata does not by itself make an article relevant.

| Publisher | Public feed URL | Source type / coverage |
| --- | --- | --- |
| NVIDIA Newsroom | <https://nvidianews.nvidia.com/rss.xml> | Official newsroom; AI semiconductors |
| AMD Investor Relations | <https://ir.amd.com/news-events/press-releases/rss> | Official company IR; AI semiconductors; AMD |
| Equinix Investor Relations | <https://investor.equinix.com/news-events/press-releases/rss> | Official company IR; data centers; EQIX |
| U.S. Department of Energy | <https://www.energy.gov/rss.xml> | Government; energy and power |
| U.S. Energy Information Administration, Today in Energy | <https://www.eia.gov/rss/todayinenergy.xml> | Government energy information; energy and power |
| Data Center Dynamics | <https://www.datacenterdynamics.com/en/rss/> | Industry publisher; data centers, networking, cooling, energy, and compute when feed text matches |
| The Robot Report | <https://www.therobotreport.com/feed/> | Industry publisher; robotics |

These URLs are public feeds, not scraped pages or private endpoints. The feed registry is intentionally small and editable. Confirm an endpoint and its publisher terms before adding or changing a source. Feeds may change, become unavailable, or omit entries; a failed source is logged, and the collector aborts without writing if all sources fail.

### Schedule and manual execution

`.github/workflows/research-collection.yml` runs daily at **13:17 UTC** and also supports the GitHub Actions **Run workflow** (`workflow_dispatch`) control. The job has a 15-minute timeout, runs the offline tests, then collects feeds. When the queue changes, it opens or updates the existing **draft** pull request on `automation/research-queue`; that remains the human review gate for collected research and is not automatically merged. The workflow pins the supported `ubuntu-24.04` runner image and Node.js 24-compatible action releases. The Python collector uses only the standard library, has a 20-second request timeout, retries transient network/server failures up to two times, and limits feed responses to 5 MiB.

The workflow uses GitHub-hosted runners and the repository's included Actions allowance; review the repository's GitHub billing quota to keep usage within the $0 budget. It uses the built-in `GITHUB_TOKEN`, not a personal access token or API key. Repository Actions settings must allow workflows to create pull requests.

Run the same process locally with Python 3.12 or newer:

```sh
python -m unittest discover -s research -p "test_*.py" -v
python research/collect.py
```

For a one-off run with another output file, use `python research/collect.py --output path/to/queue.json`. Scoring rules are configurable in `research/relevance-rules.json`; use `--rules path/to/rules.json` to test a separate rules file. The active queue is sorted by relevance score descending, then publication recency, and the combined active and excluded queue retains at most 500 records. Entries are deduplicated by tracking-parameter-normalized canonical URL and namespaced feed identifier. Repeated items retain their original IDs, URLs, publication and retrieval dates, and review decisions, so unchanged runs do not rewrite the JSON file.

### Relevance scores

Each item receives an explainable 0–100 `relevance_score`, a `priority`, `relevance_reasons`, an `event_type`, `mentioned_companies`, and an `exclusion_reason` when excluded. `affected_tickers` remains provisional and exact-match-only; ticker aliases are matched case-sensitively, and official investor-relations feeds may supply their own company ticker. Company mentions alone do not make an item eligible.

`research/relevance-rules.json` holds the category weights, signal weights, phrase patterns, and 365-day stale threshold. A matched infrastructure topic contributes 26–35 points; explicit compute linkage contributes 20; a concrete major development contributes 22; measurable scale/investment detail contributes 15; and a named public company contributes 8. A topic must match before those signals count. Scores are capped at 100. Evergreen explainers are capped to low priority (40–59). Compute-link matching includes AI accelerators/GPUs, inference, rack-scale compute, and energy or nuclear projects explicitly tied to AI infrastructure. Gaming language excludes an item only when no covered infrastructure topic or compute connection matches, so a passing gaming reference does not suppress a qualifying industrial AI story. Sponsored/promotional items, items over 365 days old, general energy coverage without a compute connection, and items scoring 0–39 are excluded with a reason.

| Classification | Score | Queue handling |
| --- | ---: | --- |
| High | 80–100 | Eligible, highest-priority review |
| Medium | 60–79 | Eligible, monitor |
| Low | 40–59 | Eligible background coverage |
| Excluded | 0–39 | Removed from active review; retained under `excluded_items` |

The score is a rules-based triage signal, not financial analysis. It does not invent figures or call an LLM. Categories and company names are inferred from feed text, may be incomplete, and require a human to verify the source article.

### Human review and approval

There is no publicly deployed admin route or write API in the static Cloudflare Pages site. The editorial dashboard is a separate, local-only Python app; run it from the repository root:

```sh
python research/review_server.py
```

It binds only to `127.0.0.1`, requires a matching local origin and per-session CSRF token for reads and writes, and uses the configured `git config user.name` as the reviewer identity. Approve, reject, hold, and reconsider actions append timestamped decisions and optional notes to the separate, version-controlled `research/data/editorial-decisions.json`. The dashboard never edits the generated queue; decisions remain after a page refresh because the inbox reloads the separate JSON decision log. The collector only reads and writes `research/data/research-queue.json`, so it cannot overwrite the editorial log. Commit and push local decisions through your normal authenticated Git workflow.

For a GitHub-hosted review request, open **Actions → Submit editorial review decision → Run workflow**. Supply the stable article ID, choose approve, reject, hold, or reconsider, and optionally add notes. GitHub restricts manual dispatch to authorized repository users; the workflow validates the ID against the active `items` array in the designated `automation/research-queue` branch, without checking out or merging that draft branch. Excluded and nonexistent IDs are rejected. The decision log is read from `main`, appended to, and proposed in a separate pull request targeting the default branch; review the article and audit entry, then merge that decision PR manually. The collector continues to update only the research queue, leaving the editorial decision history intact. Neither workflow automatically merges or publishes research.

The inbox includes only eligible active queue items; excluded records remain in the collector's audit archive and cannot be approved by the workflow. It supports text search, category/priority/company/status filters, relevance/date sorting, full available article metadata, recorded scoring factors, and decision history. Reconsidering an approved, rejected, or held item returns it to awaiting review while preserving prior decisions. Repeated workflow runs with the same run ID are idempotent.

The local dashboard is intentionally unavailable in the public static build: the Astro build includes neither its UI nor research queue/decision data, and the local server is loopback-only. It writes only on the machine where it is running; GitHub workflow submissions are the authenticated route for version-controlled PRs. Each dispatch creates a separate PR; overlapping unmerged decision PRs can conflict in the shared JSON file and must be resolved by a human. Article approval remains editorial metadata only until a separately approved publication phase.

## Phase 5: approved intelligence publishing

The public `/intelligence/` feed, approved-article detail pages, and company intelligence pages are generated from a build-time allowlisted export. `npm run dev`, `npm run check`, and `npm run build` run `research/export-approved-content.mjs` before Astro. The exporter reads only the active `items` array in `research/data/research-queue.json` and the authoritative append-only `research/data/editorial-decisions.json` log. For each stable article ID, only its latest recorded `approve` decision with status `approved` qualifies. Queue `review_status`, relevance score, legacy status, and source feed metadata cannot grant approval; excluded archive records are not considered. A reconsider, hold, reject, or missing decision leaves the article unpublished.

The exporter writes an ignored, generated file at `src/data/approved-research.generated.json`. It does not modify the queue or decision log, so builds cannot change editorial decisions. Only an explicit field allowlist is exported; reviewer identity, notes, request IDs, and decision history never enter the public build. Stable IDs, original and canonical source URLs, feed provenance, publication/retrieval times, article metadata, and excerpts are retained where available. Invalid source URLs are reported and omitted. Duplicate stable IDs and canonical source URLs are published at most once. Tickers from a source registry entry appear as source-confirmed mappings; inferred ticker values remain separately labeled as candidates.

The public feed supports search, category and company filters, and relevance/date sorting without a client-side GitHub credential or write endpoint. Article detail pages and company views are statically generated only from the approved export. The site publishes the source-provided headline and excerpt, not generated analysis or full article text. Cloudflare Pages continues to use `npm run build` and `dist`; ensure the build checkout contains the reviewed decision log. No deployment secrets are needed, no PR is merged automatically, and the existing human-reviewed editorial workflow remains unchanged.

### Scoring follow-up identified during publishing work

`research/collect.py:parse_feed` cleans the feed title and excerpt, falls back to the title when the feed description is empty, then sends that text through `classify_entry` and `score_article`. The checked-in scoring algorithm and rules are unchanged in this phase. The added production-path regressions document these gaps: the AMD–Anthropic two-gigawatt RSS headline has no description and is excluded because the AI-semiconductor topic patterns do not match its GPU wording without an explicit AI/compute phrase; the Equinix–NVIDIA inference headline has no description and scores 63/100 (medium) because “accelerates” is not a major-event pattern; the Amazon nuclear-project article is classified as data-center coverage but scores 43/100 (low) because the compute-link regex does not match plural “data centers”; and NVIDIA’s industrial-AI newsroom item is classified as robotics but scores 36/100 (excluded) because no compute-link or major-event signal matches. The Amazon/AEP/X-energy scenario also has no direct AEP or X-energy entry in the current company alias table and no dedicated source feed in `research/sources.json`.

These are collector/scorer limitations, not publishing approvals. A separate scoring/source-coverage change should propose narrowly scoped patterns for GPU deployments, inference infrastructure, industrial AI/robotics, and SMR-to-compute links, and add verified company aliases or feeds as appropriate. That work should review its score and exclusion changes independently; this publishing phase must not alter relevance weights or promote articles automatically.

### Data shape and limitations

Each finding includes a stable feed ID, canonical article URL, title, publisher, article `source_url`, configured RSS/Atom `feed_url`, publication and retrieval timestamps in UTC, one category (or `other`), ticker candidates, mentioned company names, a short text excerpt from the feed, relevance metadata, and a compatibility `review_status` field. Editorial decisions and their audit trail live separately in `research/data/editorial-decisions.json`. When no reliable source excerpt is present, the title is used.

This is feed collection and deterministic triage, **not AI-generated research**. Entries without a valid article URL, title, or publication time are skipped with a log message; unrelated entries are instead retained as excluded records. Sponsorship and staleness detection rely on feed text and publication dates and can miss undisclosed promotions or incorrectly dated articles. Sources can omit context, republish links, or provide inaccurate metadata. Partial source outages are reported in the job log; any resulting queue change must be checked for source coverage before approval. The queue is version-controlled research intake, not live market data or an investment recommendation. No API keys, paid APIs, or additional hosted services are used.
