# Songuess

Songuess is an unlimited, solo music guessing game. A round opens one Apple preview in continuous stages—1, 2, 4, 7, 11, and 15 seconds—while the browser owns all temporary game state.

The runtime frontend and backend read a local catalog built offline from public ListenBrainz, MusicBrainz, and Apple data. Gameplay itself never calls those services.

## Repository map

```text
.
├── backend/
│   ├── app/                 # FastAPI, validation, SQL access, DB initializer
│   ├── pyproject.toml
│   └── uv.lock
├── frontend/
│   ├── src/components/      # React game island
│   ├── src/pages/           # Astro shell
│   ├── package.json
│   └── pnpm-lock.yaml
├── migrations/              # Ordered, tracked SQLite/D1-compatible schema changes
├── dataset/                 # Resumable offline catalog importer
├── .env.example
└── plan.md
```

The API is stateless. There are no game, account, history, or session tables. Songs may belong to many normalized genres through `song_genres`.

## Prerequisites

- Python 3.13 or newer
- [`uv`](https://docs.astral.sh/uv/)
- Node.js
- [`pnpm`](https://pnpm.io/)

## Backend

Install the locked Python environment and create the SQLite database:

```bash
cd backend
uv sync
```

Run the API:

```bash
just dev
```

FastAPI applies the idempotent schema migration during startup. By default the ignored local database is created at `backend/data/songuess.sqlite3`. Copy `.env.example` to `.env` and export its values if a different path is needed; the application does not load dotenv files implicitly.

Available runtime endpoints:

- `GET /api/health`
- `GET /api/filters`
- `POST /api/round`
- `GET /api/songs/search?q=...`
- `GET /api/songs/{id}`

Population is incremental: `--target-total` is the desired final enabled-song count, not the
number of songs to add. For example, to reach exactly 5,000 validated songs from 1950 through
2026 while preserving the current catalog:

```bash
just dataset populate --target-total 5000 --candidates 11000 --year-min 1950 --year-max 2026
```

Discovery and popularity scoring are separate pipelines. Discovery seeds recording MBIDs from
paginated ListenBrainz sitewide charts and expands popular artists through token-free direct-artist
LB Radio results. When a ListenBrainz token is configured, `top-recordings-for-artist` is tried
before the public fallback; similar-artist LB Radio remains an explicitly requested diversity
source. Discovery counts are discarded. The importer then enriches exact
recording and artist identities through a resumable MusicBrainz SQLite cache, confidently matches
Apple tracks, validates previews with bounded concurrency, and performs idempotent upserts
deduplicated by both MusicBrainz recording ID and Apple track ID. It never merges recordings by
fuzzy title.

Apple matching prefers the least-censored identity-equivalent result in this order: `explicit`,
unrated/`notExplicit`, then `cleaned`. When ordinary search exposes only a clean track, the importer
inspects Apple Music's structured Other Versions links and accepts an explicit alternate only when
title, credited artist, and duration remain an exact-compatible match. Run
`just dataset populate --backfill-explicit-versions` from `backend/` to resumably refresh the entire catalog; completed
checks are retained for 30 days and validated replacements update the Apple link and preview
without changing Spotify counts.

ListenBrainz currently requires a user token for `top-recordings-for-artist` traffic. The token is
optional: when absent, the importer expands the same public popular-artist seeds through LB Radio
with similar-artist expansion disabled. Requests send `LISTENBRAINZ_TOKEN`, when configured, only to
`api.listenbrainz.org`. ListenBrainz is used only to discover candidate recording identities; its
counts never enter the songs table or popularity score. Small resumptions bound enrichment to the
remaining gap and skip fresh negative Apple matches instead of repeatedly processing rejects.

Spotify web stream counts are the catalog's single popularity source. The idempotent Spotify
backfill reuses existing exact relationships and searches by cached MusicBrainz ISRC. Retry passes
first inspect exact MusicBrainz URL relationships, then use a public metadata catalog only to
resolve a Spotify URL by exact ISRC or exact title + credited artist + near-identical duration.
The fallback catalog's popularity is never read or stored. The backfill reads the lifetime count
from Spotify's browser-hydrated `getTrack` response, stores the count and
retrieval timestamp, and assigns a tie-aware 0–100 percentile across every enabled song with a
count. Missing values remain unavailable rather than becoming zero. Reruns skip fresh complete
rows, retry failures, and safely include songs added by later 25,000- or 100,000-song population
runs.

Artist-origin countries come only from the explicit `country` field on every credited MusicBrainz
artist. Songs may have multiple origins through normalized `countries` and `song_countries` tables;
missing country metadata remains missing. Run `just dataset populate --backfill-countries` from `backend/` to resumably
attach countries to an existing catalog without changing songs or popularity rankings.

Genre classification treats Apple's structured primary genre as the baseline and admits at most
two supplementary MusicBrainz community-tag genres. Supplementary tags are processed individually,
must have positive votes with meaningful absolute and relative support, and use exact curated
mappings instead of substring matches. Stored genre evidence retains rank, source, and score. Run
`just dataset populate --audit-genres` to write a dry-run report under the ignored dataset cache,
then run `just dataset populate --backfill-genres` to rebuild the local catalog from existing Apple
and MusicBrainz caches.

Run `just dataset spotify_streams_browser` from `backend/` after extending the catalog. It never replays
captured tokens or retains browser headers, cookies, and response bodies. This is an unofficial
Spotify web workflow, so search or hydration changes become explicit retryable failures rather
than silently assigning zero. Every enabled song is expected to have Apple Music and Spotify
destinations before release; both are shown as listening actions on the reveal screen.

Use `just dataset populate --help` for custom totals and ranges. `just dataset verify --snapshot
<path>` records the current identities, while `just dataset verify --target-total <count>
--preserve-snapshot <path>` checks exact totals, preservation, unique IDs, previews, and the
unmodified year distribution. No token is required for the default discovery path. MusicBrainz
remains at one request per second and Apple remains below 20 requests per minute.

## Frontend

In another terminal:

```bash
cd frontend
pnpm install --frozen-lockfile
just dev
```

Open the URL printed by Astro. Its development server proxies relative `/api/...` requests to `http://127.0.0.1:8000`, matching the intended same-origin production request shape without hard-coding a backend host in application code.

The React island implements:

- multi-genre, year, and popularity setup filters;
- uniformly random SQL-backed round requests with recent-song exclusion;
- debounced, catalog-only guess search;
- six continuous snippet boundaries with pause, resume, and rewind;
- wrong-guess and skip progression without replaying earlier audio;
- immediate reveal, correct, and exhausted-attempt outcomes;
- unrestricted play, pause, and replay of the full preview after reveal;
- clean Next Song reset with the active filters preserved;
- visible loading, empty-catalog, no-match, and audio failure states.

## Checks

Each service exposes the same task-runner interface. `lint` formats and checks the code, while
`test` runs the configured test suite:

```bash
cd backend
just lint
just test

cd ../frontend
just lint
just test
```

## Catalog contract

The importer inserts real, validated rows only; `preview_url` is required and fake Apple preview URLs are never used. Runtime gameplay reads stored preview URLs and never calls Apple, ListenBrainz, or MusicBrainz. The local database and API response caches are ignored because they are reproducible development artifacts.

## Incremental catalog operations

The offline catalog pipeline is split into append-only operations:

```bash
just dataset cache_maintenance baseline
just dataset cache_maintenance cleanup                 # dry run
just dataset cache_maintenance compact
just dataset cache_maintenance cleanup --apply
just dataset catalog_pipeline discover --target-total 10000
just dataset catalog_pipeline populate --target-total 10000 --checkpoint-target 6000
just dataset catalog_pipeline refresh
just dataset catalog_pipeline verify --target-total 10000
just dataset catalog_pipeline export-delta
just dataset catalog_pipeline evaluate
```

Discovery stores ranked identities, source evidence, artist-representation penalties, and the
reason each recording was included before MusicBrainz or Apple enrichment. Population consumes
that manifest and skips every known MusicBrainz and Apple identity; it never rewrites existing
songs merely because the target grows. Refresh changes mutable Spotify count fields separately.
Snapshots hash all application-facing baseline song fields except mutable popularity data, so
verification detects unintended changes to the original catalog.

Provider caches and telemetry remain local. `export-delta` contains only new songs, required
dimension rows, and relationship rows for D1; it explicitly excludes raw provider responses,
candidate manifests, checkpoints, and metrics. `cleanup-caches` is evidence-gated and defaults to
a dry run. Apple JSON is removable only after every track and artist-search key exists in the
compact SQLite cache.

The 10,000-song manifest targets 5,000 popular artists and 15 recordings per artist across several
ListenBrainz windows and token-free direct-artist LB Radio expansion. A configured
`LISTENBRAINZ_TOKEN` enables the authenticated per-artist chart as a preferred source, but is not
required. An insufficient manifest cannot be populated; increase the candidate or per-artist
limits and rerun the `catalog_pipeline discover` operation before enrichment.

## Cloudflare Worker deployment

Production uses one Python Worker. Requests under `/api/*` run through FastAPI and the request's
`env.DB` D1 binding; all other requests are served directly from `frontend/dist` by Workers Static
Assets. Local Uvicorn development continues to use `backend/data/songuess.sqlite3` through the same
async repository contract.

The runtime search tables in migration `012_runtime_search.sql` keep D1 queries bounded. FTS5
returns at most 500 candidates for Python ranking, recent-song exclusions use one JSON binding even
for 500 IDs, and random rounds use an exact count plus random offset instead of
`ORDER BY RANDOM()`.

### Local Worker validation

Install the locked dependencies, build the frontend, migrate local D1, and start the real Workers
runtime:

Use Python 3.13, `uv` 0.12.3 or newer, and Node.js 22 LTS for the Python Workers toolchain.

```bash
cd backend
uv sync --locked --dev
cd ../frontend
pnpm install --frozen-lockfile
cd ..
just build
just d1-migrate-local
just worker-dev
```

Wrangler persists local D1 state under the ignored `.wrangler/` directory. To exercise a catalog,
generate and import the application-only seed before starting the Worker:

```bash
just export-d1
just d1-import-local release/catalog.sql
```

The exporter refreshes the checked-in `release/catalog.sql` and `release/catalog.manifest.json`.
The manifest contains table counts plus deterministic SHA-256 hashes over the enabled application
rows and generated SQL. The SQL includes only enabled songs, referenced dimensions and
relationships, genre evidence, and derived runtime search rows. Provider caches, browser telemetry,
candidate manifests, checkpoints, metrics, and Spotify backfill failures are excluded.

### Preview and production resources

The initial Cloudflare resources are live:

- Preview Worker: `https://songuess-preview.bruno-farfan-miquel.workers.dev`
- Production Worker: `https://songuess.bruni.to`
- Preview D1: `songuess-preview` (`97efbc35-917c-40da-9354-5351de80b73a`)
- Production D1: `songuess-production` (`7b19809e-0573-4ef5-b419-556456d1ed5e`)

For subsequent catalog releases:

1. Generate and verify a fresh application-only seed and manifest.
2. Apply migrations explicitly with `wrangler d1 migrations apply <database> --remote --env
   <preview|production>`.
3. Import a verified seed explicitly with `wrangler d1 execute <database> --remote --env
   <preview|production> --file release/catalog.sql`.
4. Compare D1 table counts with the generated manifest, deploy preview, and complete the gameplay
   and audio smoke tests before approving production.

The root Wrangler file documents the complete deployment; the backend mirror is required because
`pywrangler` resolves its project beside `backend/pyproject.toml`. Keep their deployment values in
sync.

Ordinary Worker deployment never migrates or replaces catalog data. `staging` is the default
integration branch: every push runs backend and frontend checks, then deploys the preview Worker.
Production releases are pull requests from `staging` into the protected `production` branch; a
successful push there deploys the production Worker. Preview deployments cancel stale runs, while
production deployments run serially. The manual deployment workflow remains available for retries,
but requires the selected Git ref and environment to match (`staging`/`preview` or
`production`/`production`).

D1 migrations and catalog imports remain explicit operations. The migration workflow uses the same
branch/environment guard, and no deployment imports or replaces catalog data. GitHub Environments
also restrict preview and production deployments to their matching branches.

`CLOUDFLARE_ACCOUNT_ID` is configured in GitHub; add a scoped `CLOUDFLARE_API_TOKEN` repository or
environment secret before invoking a remote workflow.

The production Custom Domain is managed by Wrangler and Cloudflare automatically provisions its
DNS record and certificate. Use Workers Logs and D1 query metadata to inspect errors, CPU time,
duration, and `rows_read`; catalog imports remain a separately approved operational action.
