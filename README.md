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
├── migrations/
│   └── 001_initial.sql      # SQLite/D1-compatible catalog schema
├── dataset/                 # Resumable offline catalog importer
├── .env.example
└── plan.md
```

The API is stateless. There are no game, account, history, or session tables. Songs may belong to many normalized genres through `song_genres`.

## Prerequisites

- Python 3.11 or newer
- [`uv`](https://docs.astral.sh/uv/)
- Node.js
- [`pnpm`](https://pnpm.io/)

## Backend

Install the locked Python environment and create the SQLite database:

```bash
cd backend
uv sync
uv run python -m app.database
```

Run the API:

```bash
just dev
```

FastAPI also applies the idempotent schema migration during startup, so the explicit initialization command is optional. By default the ignored local database is created at `backend/data/songuess.sqlite3`. Copy `.env.example` to `.env` and export its values if a different path is needed; the application does not load dotenv files implicitly.

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
just populate-5000
```

The importer merges ListenBrainz's precomputed top 1,000 sitewide charts from all-time through
weekly windows with recordings from up to 1,000 naturally ranked artists, then globally ranks the
deduplicated candidates by listen count. It enriches recording IDs through a resumable MusicBrainz
SQLite cache, confidently matches Apple tracks, validates previews with bounded concurrency, and
performs idempotent upserts deduplicated by both MusicBrainz and Apple IDs. Cached negative Apple
matches expire, transient preview failures remain retryable, and popularity is normalized over the
entire enabled catalog. No decade quotas or weights are applied.

Artist-origin countries come only from the explicit `country` field on every credited MusicBrainz
artist. Songs may have multiple origins through normalized `countries` and `song_countries` tables;
missing country metadata remains missing. Run `just backfill-countries` from `backend/` to resumably
attach countries to an existing catalog without changing songs or popularity rankings.

Use `just populate --help` for custom totals and ranges. `just snapshot-catalog <path>` records the
current identities, while `just verify-catalog --target-total <count> --preserve-snapshot <path>`
checks exact totals, preservation, unique IDs, previews, and the unmodified year distribution. The
`populate-25000` recipe resumes toward the full catalog. No API credentials are required;
MusicBrainz remains at one request per second and Apple remains below 20 requests per minute.

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

The schema and query style are SQLite/D1-compatible, frontend requests are relative, and Astro builds static assets. This keeps the code structurally ready for a later Cloudflare adaptation, but this repository contains no Cloudflare deployment configuration, infrastructure, secrets, domains, CI/CD, or deployment instructions.
