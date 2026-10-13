# Songuess

Songuess is a music guessing game with unlimited solo play and asynchronous challenges. A round opens one Apple preview in continuous stages—1, 2, 4, 7, 11, and 15 seconds. Solo rounds use browser state; challenges save progress on the server.

The runtime frontend and backend read a local catalog built offline from public ListenBrainz, MusicBrainz, and Apple data. Gameplay itself never calls those services.

## Repository map

```text
.
├── backend/
│   ├── src/app/             # FastAPI, validation, SQL access, DB initializer
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

There are no user accounts or passwords. Challenge rooms, their frozen songs, participants, and round progress live in SQLite/D1. Songs may belong to many normalized genres through `song_genres`.

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

## Asynchronous challenges

Choose **Multiplayer**, then **Create challenge**. Pick a mix or set custom filters, then enter a display name and choose
1–20 songs and a 1–24-hour duration (defaults: 10 songs and 24 hours). Creation freezes
distinct songs and their order, joins the creator, and produces a code and an invite such
as `/?room=ABCDEFGH`. There is no lobby: up to 50 players can join and play independently
until the deadline, even when everyone who previously joined has finished.

Every song allows one full six-clue round. The server records guesses, skips, reveals,
and results. Reopening the invite in the same browser resumes saved progress. Correct
answers earn 6–1 points according to the clue; giving up or exhausting clues earns zero.
Standings are visible during play and refresh while the room panel is visible. Click a
player's row to expand their per-song points; opening another player closes the previous
row. Unplayed songs are marked separately from failed guesses. Ties use
the number of correct answers, then clue-1 solves through clue-6 solves; equal results
share a rank. Other players' song answers are not included in standings.

Participants preload the frozen playlist, including reveal details, and warm upcoming
audio, artwork, and scene assets. Clues, guesses, reveals, and the next song update
immediately while versioned actions save in order in the background. The server still
checks the deadline and calculates scores; a failed or conflicting save reloads saved
progress before play continues. Playlist answers are available to participants in browser
tools, just as in solo play; this casual challenge favors fast playback over hiding answers.

A signed, HttpOnly guest cookie identifies the browser for 30 days. Names are display
labels: changing one cannot create another entry in the same room. Clearing cookies or
switching browsers loses this identity and can create another entry. This is a casual
competition; IP addresses do not determine identity, so friends on the same Wi-Fi can
play separately. Public solo catalog/reveal endpoints remain available.

Challenge endpoints:

| Method | Path | Purpose |
| --- | --- | --- |
| POST | `/api/guest-session` | Create or renew the guest cookie |
| POST | `/api/rooms` | Freeze a room and join its creator |
| GET | `/api/rooms/{code}` | Room summary and leaderboard |
| POST | `/api/rooms/{code}/join` | Join or recover existing participation |
| GET | `/api/rooms/{code}/me` | Saved progress, deadline, and frozen playlist for prefetching |
| POST | `/api/rooms/{code}/actions` | Versioned guess, skip, or give-up action |

After the deadline, joining and scoring stop immediately, and unfinished songs count
as zero points. Results remain available for seven days after closure. The hourly Worker
schedule deletes expired rooms and cascades through their songs, participants, and
progress; API access stops at the deletion time even if cleanup has not run yet. Local
SQLite cleanup uses the same implementation:

```bash
just rooms-cleanup
```

Configure `SONGUESS_GUEST_SECRET` as a Worker secret separately for preview and production,
using a random value of at least 32 characters. Keep it stable across deployments; changing
it invalidates guest cookies. Local Uvicorn has a development fallback; `.env.example`
documents the override. For local Worker development, copy `backend/.dev.vars.example`
to the ignored `backend/.dev.vars` and replace its placeholder with a random secret.
Apply migration `015_challenge_rooms.sql` before deploying the feature. Catalog exports
exclude rooms and participant data, and catalog replacement preserves room snapshots.

## Blender assets

The shared album sleeve and vinyl models live in [`blender/`](blender/README.md),
with an editable Blender source and reveal reference animation. Run
`just blender-build` to regenerate the small GLB exports and preview. The game loads these assets in the browser: the record spins during playback,
keeps its rotation through the reveal, and follows playback afterward. The reveal applies
the current album artwork and follows the Blender reference using TypeScript.
The carousel reuses the same sleeve model, folding old covers back and lifting
the selected cover forward.
Rotation pauses in hidden tabs and respects reduced motion preferences.

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
candidate manifests, checkpoints, and metrics. After applying every delta row, execute its
`catalog_runtime_refresh_sql` field on the destination D1 database. This rebuilds the runtime
snapshot from the whole destination catalog, including existing songs, and changes its version.
Full SQL exports perform the same refresh after replacing the catalog. `cleanup-caches` is
evidence-gated and defaults to a dry run. Apple JSON is removable only after every track and artist-search key exists in the
compact SQLite cache.

The 10,000-song manifest targets 5,000 popular artists and 15 recordings per artist across several
ListenBrainz windows and token-free direct-artist LB Radio expansion. A configured
`LISTENBRAINZ_TOKEN` enables the authenticated per-artist chart as a preferred source, but is not
required. An insufficient manifest cannot be populated; increase the candidate or per-artist
limits and rerun the `catalog_pipeline discover` operation before enrichment.

### Existing-artist catalog expansion

`dataset.artist_expansion` resumably expands only artists represented in the immutable 10,000-song
baseline. Authenticated ListenBrainz top recordings provide the priority queue, canonical
MusicBrainz studio discographies provide completeness, and Spotify web playcounts remain the sole
acceptance and ranking metric. The pipeline excludes alternate versions by default, counts
collaborations toward every canonical credited artist, applies a 200 million stream floor and a
30-song artist cap, and preserves all baseline rows.

Run it through the generic backend dataset command; for example:

```bash
cd backend
just dataset artist_expansion status
just dataset artist_expansion pipeline --target-total 20000
```

The versioned SQLite checkpoint records progress per artist and candidate, so the operation can be
stopped and resumed without rebuilding completed work. Reports, provider caches, browser state and
checkpoints remain ignored local artifacts. Use the Kanye pilot before changing the discovery
algorithm or acceptance rules.

## Cloudflare Worker deployment

Production uses one Python Worker. Requests under `/api/*` run through FastAPI and the request's
`env.DB` D1 binding; all other requests are served directly from `frontend/dist` by Workers Static
Assets. Local Uvicorn development continues to use `backend/data/songuess.sqlite3` through the same
async repository contract.

The runtime search tables in migration `012_runtime_search.sql` select at most 500 FTS5 candidates
using `ORDER BY rank`. SQL computes exact/prefix/substring/token priorities from indexed normalized
fields; Python reranks at most 40 fuzzy candidates with memoized field similarities. This retains
typo matching while bounding CPU work. Fuzzy ties can differ from the old full-pool ranking.
Seeded browsing uses a cached compact ID array and a coprime arithmetic permutation; pages stay
stable and disjoint without repeated cryptographic hashing. Catalog pages load on navigation.

Migration `014_runtime_catalog.sql` backfills a versioned, compact catalog snapshot into the
single `catalog_runtime` row. Initial metadata projects only its small facet lists and reads the
two indexed year endpoints, avoiding full snapshot decoding. D1 caches the full snapshot for each
Worker isolate cache window; browse totals, contextual facets, and eligible round IDs use it without
catalog scans. Each round applies recent-song exclusions in memory, chooses uniformly at random,
and fetches only the selected preview by primary key. Facets cache only their actual upstream
dependencies in at most 128 integer bitmasks. Year/popularity buckets and facet intersections use
C integer operations instead of scanning matching song IDs for each slider value.

The result cache holds at most 128 entries and 30,000 result rows for five minutes. Worker cache
misses load in the requesting task so binding I/O remains within that request's lifetime;
concurrent cold requests can each read the one-row snapshot.
Local SQLite stays uncached and uses indexed existence checks for genre/country facets. Catalog
updates become visible on cache expiry or a Worker restart. Full exports refresh the snapshot
after all catalog rows are inserted; direct SQL edits also require the refresh statement from
`014_runtime_catalog.sql`. Apply migration 014 before deploying this runtime.

The frontend caches normalized contextual requests across wizard remounts and commits sliders on
release. Song search requires two characters with a 350ms debounce, while an empty query browses
the catalog. One next round is prefetched only after playback starts or the current round is
revealed; carousel tail pages are no longer fetched speculatively.

### Local Worker validation

Install the locked dependencies, build the frontend, migrate local D1, and start the real Workers
runtime:

Use Python 3.14, `uv` 0.12.3 or newer, and Node.js 22 LTS for the Python Workers toolchain.
The compatibility date must be `2026-09-08` or later to select the Python 3.14 runtime with upstream
memory-leak fixes. The Worker tooling and Pydantic locks include compatible CPython 3.14 WASM wheels.

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
