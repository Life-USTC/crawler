# Scheduled Incremental News Crawl + Sync — Design

Date: 2026-09-11
Status: Approved
Repo: crawler (this repo only; no changes to the server repo)

## Goal

Run `crawl --incremental` + `sync` periodically and unattended so new USTC
news reaches the Life-USTC platform without a human invoking the CLI.

## Decision summary

| Question | Decision |
|---|---|
| Where does it run? | GitHub Actions scheduled workflow |
| What runs? | Existing `ustc-crawler crawl --incremental` then `ustc-crawler sync` — no crawler package code changes |
| Cadence | Every 6 hours via cron, plus `workflow_dispatch` |
| State store | GitHub Actions cache (mutable state), release asset (seed bootstrap) |
| Seed source | One-time **uncommitted** local prune script → `crawler-seed.sqlite.zst` → `gh release upload` |
| Archive (`data/pages`, `data/media`, …) | Not persisted in CI; full crawls/reindex stay local |

## Why not the alternatives

- **Cloudflare Containers + cron trigger**: works without a rewrite, and the
  included allotment would likely cover incremental runs, but the user judged
  the added paid-plan surface not worth it versus free CI minutes.
- **Python Workers (Pyodide)**: cannot run this codebase — no filesystem, no
  SQLite, no long-running CPU, C-extension deps (lxml) unsupported.
- **Local cron**: zero cost but requires this machine to be on.

## Current-state facts this design relies on

- `crawl --incremental` already computes per-source cutoffs from
  `MAX(articles.published_at)` and re-enqueues only seeds/listings
  (`Store.source_newest_dates`, `Store.reset_seeds_and_listings`).
- Sync is idempotent and durable: content-addressed `event_id`s, transactional
  outbox (`sync_outbox` → `sync_batches`), safe to replay or re-send.
- The local 17 GB `data/crawler.sqlite` is mostly dead weight for incremental
  runs (measured 2026-09-11):

  | Table | Size | Cloud treatment |
  |---|---|---|
  | `sync_outbox` | 8.4 GB | keep only non-terminal rows (`pending`/`batched`/`uploading`) |
  | `links` (+index) | 5.0 GB | drop all rows |
  | `articles` | 2.6 GB | keep (cutoffs + dedup) |
  | `pages` | 273 MB | keep |
  | `frontier` | 111 MB | keep (seen-set) |
  | `failures`, `runs` | small | drop all rows |
  | rest | ~400 MB | keep |

  Pruned + `VACUUM` ≈ 3.3 GB raw; zstd-compressed ≈ 0.7–0.9 GB (fits the
  10 GB Actions cache with room for several generations, and the 2 GB release
  asset limit).

## Components

### 1. `.github/workflows/incremental-sync.yml` (only committed change)

Jobs, in order, single job with a concurrency group (`incremental-sync`) so
runs never overlap:

1. Checkout; install `uv`; `uv sync --locked`.
2. Restore state:
   - `actions/cache/restore` with key prefix `crawler-state-`
     (`restore-keys` semantics: newest matching entry wins).
   - On cache miss, download `crawler-seed.sqlite.zst` from the
     `crawler-state-seed` release asset via `gh release download`.
   - Decompress to `data/crawler.sqlite`.
3. `uv run ustc-crawler db-upgrade` (idempotent; guards against schema drift).
4. `uv run ustc-crawler crawl --incremental`.
5. `uv run ustc-crawler sync --server "$CRAWLER_SERVER"` (secret
   `USTC_CRAWLER_INGESTION_SECRET` from env, per existing CLI contract).
6. Save state **even if crawl/sync failed** (`if: always()`): compress DB with
   zstd, `actions/cache/save` under a fresh key `crawler-state-<run_id>`.
   Cache LRU eviction discards older generations automatically.
7. On failure: upload crawl/sync logs as a run artifact.

Timeout: 45 minutes on the job. `shell` defaults and `uv` caching per the
existing `ci.yml` conventions.

### 2. Seed bootstrap (one-time, uncommitted)

A throwaway local script (never committed):

1. `VACUUM INTO` a copy of `data/crawler.sqlite`.
2. Delete all rows from `links`, `failures`, `runs`.
3. Delete terminal `sync_outbox` / `sync_batches` / `sync_batch_items` rows
   (statuses `acked`, `failed`, `superseded`, `partial`).
4. `VACUUM`, zstd-compress, `gh release create crawler-state-seed` + upload.

If the cache is ever evicted (7 days idle), the workflow falls back to the
seed and simply re-crawls a window — idempotent sync makes this safe.

### 3. GitHub configuration (manual, documented in PR body)

- Secrets: `USTC_CRAWLER_INGESTION_SECRET`.
- The workflow uses the built-in `GITHUB_TOKEN` for cache and release access.
- Variables: `CRAWLER_SERVER` (platform base URL), seed release tag name if
  it differs.

## Data flow

```
cron ──► restore sqlite (cache, else seed release)
      ──► crawl --incremental ──► new articles → outbox (in sqlite)
      ──► sync ──► POST batches + objects to platform ingestion API
      ──► save sqlite (new cache entry, always)
```

Media for new articles is downloaded during crawl and uploaded during sync in
the same run, so `data/media` does not need to persist. Un-acked sync spool
(`data/sync-objects/`) is the one gap: if a run dies between crawl and sync,
the next run re-fetches those articles (frontier marks them done, but the
outbox events are still `pending` in the saved DB and re-drive delivery —
objects re-spool from re-fetched pages as needed). Accepted for simplicity.

## Error handling

- Job timeout / crash → next run resumes from the last saved cache entry;
  outbox replays pending events.
- Sync 4xx → permanent per-item failure recorded in DB (existing behavior);
  visible in next run's logs.
- Cache restore corruption → delete cache, fall back to seed (manual).

## Testing

- No new product code, so no new unit tests in the crawler package.
- Workflow validation: `actionlint` (if available) + a manual
  `workflow_dispatch` run against the seed before enabling the cron schedule.
- Verify a second run restores the cache written by the first (cache-hit log
  line) and performs a near-empty incremental crawl.

## Out of scope

- Full crawls, reindex, backfill — remain local operations.
- Server-side changes of any kind.
- Committing any seed/prune tooling.
