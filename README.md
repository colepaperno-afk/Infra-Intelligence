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

## Phase 2: research collection

The first collection milestone is a standard-library Python script that reads publicly accessible RSS/Atom feeds, filters and normalizes relevant entries, and writes an explicitly **unreviewed** queue to `research/data/research-queue.json`. It does not run AI analysis, make investment recommendations, or publish queue entries on the website. Existing illustrative data and labels remain in place.

### Curated sources

The checked-in registry is `research/sources.json`. It records each feed URL, publisher, source type, and (where applicable) a narrowly justified fallback category and official-company ticker.

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

`.github/workflows/research-collection.yml` runs daily at **13:17 UTC** and also supports the GitHub Actions **Run workflow** (`workflow_dispatch`) control. The job has a 15-minute timeout, runs the offline parser tests, then collects feeds. The Python collector uses only the standard library, has a 20-second request timeout, retries transient network/server failures up to two times, and limits feed responses to 5 MiB.

The workflow uses GitHub-hosted runners and the repository's included Actions allowance; review the repository's GitHub billing quota to keep usage within the $0 budget. It uses the built-in `GITHUB_TOKEN`, not a personal access token or API key. Repository Actions settings must allow workflows to create pull requests.

Run the same process locally with Python 3.12 or newer:

```sh
python -m unittest discover -s research -p "test_*.py" -v
python research/collect.py
```

For a one-off run with another output file, use `python research/collect.py --output path/to/queue.json`. The queue is sorted deterministically and retains at most 500 findings. Entries are deduplicated by tracking-parameter-normalized canonical URL and namespaced feed identifier. Repeated items retain their original retrieval time and review status, so unchanged runs do not rewrite the JSON file.

### Human review and approval

When the queue changes, the workflow opens or updates a **draft pull request** on `automation/research-queue`. The pull request is the review gate; there is no admin route, automatic merge, or public-page integration. Review each source article and verify its title, excerpt, date, category, and any ticker. To record a decision, edit that entry in the JSON queue (`review_status`: `approved` or `rejected`) and add `reviewed_by` and `reviewed_at` before merging. New entries are always marked `unreviewed`; an existing review decision is preserved when the collector sees the same item again.

### Data shape and limitations

Each finding includes a stable feed ID, canonical article URL, title, publisher, article `source_url`, configured RSS/Atom `feed_url`, publication and retrieval timestamps in UTC, one matched category, confidently matched ticker symbols, a short text excerpt from the feed, and `review_status`. Tickers are emitted only for exact matches against a small explicit alias list or for entries from the corresponding official investor-relations feed; there is no fuzzy entity inference. When no reliable source excerpt is present, the title is used.

This is feed collection and deterministic metadata normalization, **not AI-generated research**. Entries without a valid article URL, title, publication time, or covered category are skipped with a log message. Categories, snippets, and tickers still require human verification. Sources can omit context, republish links, or provide inaccurate metadata. Partial source outages are reported in the job log; any resulting queue change must be checked for source coverage before approval. The queue is version-controlled research intake, not live market data or an investment recommendation. No API keys, paid APIs, or additional hosted services are used.
