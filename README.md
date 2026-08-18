# Songuess

Songuess is an unlimited, solo music guessing game. A round opens one Apple preview in continuous stages—1, 2, 4, 7, 11, and 15 seconds—while the browser owns all temporary game state.

This repository currently contains the complete runtime frontend and backend over an intentionally empty catalog. Catalog ingestion, seed data, population scripts, and tests are deliberately deferred. No Apple, ListenBrainz, MusicBrainz, or Cloudflare credentials are needed to run the empty application shell.

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

Install the locked Python environment and create the empty SQLite database:

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

With the intentionally empty database, `/api/filters` reports zero songs and `/api/round` returns a clear `404 NO_MATCHING_SONGS` response.

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
`test` runs the configured test runner and exits successfully when the intentionally empty suite
has no tests yet:

```bash
cd backend
just lint
just test

cd ../frontend
just lint
just test
```

## Catalog contract and deferred work

The database is intentionally not populated. Future catalog work should insert real, validated rows only; `preview_url` is required and fake Apple preview URLs should never be used. Runtime gameplay reads stored preview URLs and never calls Apple, ListenBrainz, or MusicBrainz.

The later ingestion work remains responsible for candidate discovery and popularity from ListenBrainz, canonical metadata and genres from MusicBrainz, confident Apple matching, preview validation, normalized popularity, and idempotent catalog writes. Any credentials or rate-limit handling belong to that later offline workflow.

The schema and query style are SQLite/D1-compatible, frontend requests are relative, and Astro builds static assets. This keeps the code structurally ready for a later Cloudflare adaptation, but this repository contains no Cloudflare deployment configuration, infrastructure, secrets, domains, CI/CD, or deployment instructions.
