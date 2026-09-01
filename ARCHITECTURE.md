# Architecture

## Data flow

```text
source config / discovered units
              │
              ▼
        URL ownership routing
              │
              ▼
      persistent SQLite frontier
              │
              ▼
 robots policy → HTTP fetch → raw archive
                              │
                              ▼
                   extraction and scoring
                     │        │        │
                     ▼        ▼        ▼
                  articles   media   assets
                     │
                     ▼
          read-only dashboard / exports
```

SQLite is the source of truth for crawl state and relationships. Files under `data/` are derived archives keyed by response hashes, article URLs, or media hashes. Network access is confined to discovery, crawling, media download, and the explicit live-completeness script; reporting, reindexing, validation, export, and the dashboard are local operations.

## Modules

- `config.py`, `discover.py`: load configured sources and discover official unit sites.
- `routing.py`, `canonicalize.py`: normalize URLs and assign one deterministic source owner.
- `robots.py`, `http.py`: enforce robots policy, per-host rate limits, retries, and response limits.
- `crawl.py`: coordinate the persistent frontier and incremental crawl lifecycle.
- `extract.py`, `scoring.py`: turn HTML into page/article documents and classify index value.
- `store.py`: own the SQLite schema, archive writes, cleanup, reindexing, and exports.
- `media.py`: download media referenced by saved article bundles.
- `web.py`: expose a read-only HTML preview and JSON API.
- `cli.py`: compose the modules into user-facing commands.

## Invariants

- A URL has at most one configured source owner.
- Frontier state is resumable; an unbounded, uninterrupted completed run has no `pending` or `processing` rows.
- Every active article has exactly one URL-keyed JSON bundle and one HTML bundle.
- `articles.content_hash` is the SHA-256 of `body_text` and is not the archive identity.
- Raw page filenames match their stored response SHA-256.
- The dashboard opens SQLite read-only and never performs network requests.
- Inaccessible sources remain auditable page records instead of fabricated articles.

## Generated data

The `data/` tree can be very large and may contain copyrighted public-site content. It is intentionally outside Git. A clone starts with an empty `data/` directory and recreates all runtime state through the CLI.
