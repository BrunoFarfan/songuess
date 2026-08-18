# Codex Prompt — Absolute MVP Music Guessing Game

You are building the **absolute MVP of a Songless/Heardle-style unlimited solo music guessing game**.

Your task is to create the complete codebase and monorepo structure from scratch in the current folder, implementing the database schema, dataset tooling, backend API, frontend, and local development experience.

The application should end in a state that is **architecturally compatible with our intended Cloudflare production setup**, but **do not deploy anything and do not configure actual Cloudflare deployment, domains, CI/CD, production secrets, or infrastructure**.

The priority is:

1. simplicity,
2. maintainability,
3. a good mobile and desktop experience,
4. an end-to-end working MVP,
5. avoiding unnecessary abstractions and dependencies.

Do not add features outside the scope below.

---

# 1. Product concept

This is an **unlimited solo music guessing game**.

The user chooses a musical universe using a few filters:

* one or more genres,
* release year range,
* popularity range.

The application picks a random eligible song.

The player progressively unlocks more of the same preview:

* 1 second
* 2 seconds
* 4 seconds
* 7 seconds
* 11 seconds
* 15 seconds

The progression should behave as a **continuous reveal of the song**, not as six independent clips.

For example:

```text
Attempt 1:
play 0s → 1s

Skip

Attempt 2:
play 1s → 2s

Skip

Attempt 3:
play 2s → 4s
```

The user should therefore **not be forced to rehear previously unlocked audio every time**.

A **Rewind** control should always be available during gameplay. Rewind resets the playback cursor to the beginning of the preview, allowing the user to intentionally replay everything unlocked so far.

For example, if the current stage is 7 seconds:

```text
normal Play:
resume from current playback position up to 7s

Rewind:
cursor → 0s

Play after Rewind:
play from 0s up to 7s
```

After each unlocked section, the player can search for and select a song guess.

If the guess is wrong, the next longer section becomes available.

The player can also skip an attempt without guessing.

A **Give Up / Reveal Song** button must always be available during an active round. Pressing it immediately ends the round and reveals the song.

When the song is guessed correctly, all attempts are exhausted, or the user gives up, reveal the answer and allow the user to immediately start another round with the same filters.

The experience should feel like an **endless stream of individual rounds**, not a fixed-length game.

There are no accounts, sessions, saved progress, multiplayer features, leaderboards, daily challenges, or social features.

---

# 2. Absolute MVP scope

Implement only:

## Setup

A simple initial configuration screen containing:

* genre multi-select,
* minimum and maximum release year,
* minimum and maximum popularity,
* Play button.

Use sensible defaults so the user can press Play immediately without configuring anything.

## Gameplay

Display:

* current snippet duration/progression,
* large Play button,
* Rewind button,
* song search/autocomplete,
* selected guess,
* Guess button,
* Skip button,
* Give Up / Reveal Song button,
* previous incorrect guesses.

Playback should use the browser's native audio capabilities.

There is one Apple preview URL per song. Do **not** create separate audio files for different snippet lengths.

The playback model should maintain a cursor within the preview.

Example:

```text
current unlocked boundary = 4s
current playback cursor = 2s

Play
→ play from 2s
→ stop automatically at 4s

Skip
→ unlock 7s
→ cursor remains at 4s

Play
→ play from 4s
→ stop automatically at 7s
```

If the user presses Rewind:

```text
cursor → 0s
```

and the next Play can replay the currently unlocked portion from the beginning.

## Reveal

When the round ends, show:

* artwork,
* song title,
* artist,
* album if available,
* release year,
* whether the player guessed correctly, exhausted the attempts, or gave up,
* button to play the **complete Apple preview**,
* Next Song button.

The **Play Full Preview** control should use the same Apple preview URL but, after reveal, should no longer be restricted by the game's snippet boundaries.

Allow the user to:

* play,
* pause,
* replay,

the complete available Apple preview.

Next Song preserves the current filters.

---

# 3. Explicitly out of scope

Do NOT implement:

* authentication,
* user accounts,
* multiplayer,
* rooms,
* shared links,
* Spotify login,
* Last.fm login,
* recommendation algorithms,
* AI or LLM features,
* LangGraph,
* daily challenges,
* leaderboards,
* achievements,
* persistent scores,
* playlists,
* favorites,
* saved history,
* admin dashboard,
* WebSockets,
* Redis,
* Celery,
* queues,
* background workers,
* analytics,
* mobile applications,
* sophisticated scoring,
* popularity-weighted selection,
* balanced/random game modes.

For the MVP, song selection is simply:

> uniformly random among all eligible songs matching the filters.

Avoid building abstractions solely for hypothetical future features.

---

# 4. Technology stack

Use this stack.

## Frontend

* Astro
* TypeScript
* React
* plain CSS
* **pnpm**

Use `pnpm` as the JavaScript/frontend package manager.

Astro should provide the application shell.

The interactive game should be implemented as a **React island** within Astro.

Do not turn the project into an unnecessarily complex React SPA.

Do not use:

* Redux,
* Zustand,
* React Query,
* Tailwind,
* Material UI,
* shadcn,
* large UI libraries.

React hooks and normal component state are sufficient.

## Backend

* Python
* FastAPI
* Pydantic
* **uv**

Use `uv` for:

* Python environment management,
* dependency installation,
* locking,
* running backend commands,
* running scripts,
* running tests.

Do not introduce Poetry, Pipenv, Conda, or a separate requirements.txt-based workflow unless technically required by a dependency.

Keep the backend deliberately small and stateless.

Do not use SQLAlchemy unless there is a genuinely compelling reason.

Prefer simple explicit SQL.

## Database

The intended production database is **Cloudflare D1**, so design the SQL and persistence layer around **SQLite-compatible SQL**.

Do not rely on PostgreSQL-specific features.

For local development, use SQLite in a way that closely resembles the D1 schema and queries.

Keep database access thin enough that using a Cloudflare D1 binding later will be straightforward.

Do not configure the actual D1 deployment.

---

# 5. Intended production architecture

The code should be written with this eventual architecture in mind:

```text
Cloudflare
│
├── static Astro frontend
│   └── React game island
│
├── Python/FastAPI API
│   └── /api/*
│
└── D1
    └── song catalog
```

Frontend and backend will ultimately share the same origin.

Therefore frontend API calls should look like:

```ts
fetch("/api/round")
```

rather than depending on a hard-coded backend hostname.

The finished repository should be in a shape where adapting the Python API to the Cloudflare Python Workers/FastAPI environment and binding it to D1 is straightforward.

However:

**Do not configure, execute, or document an actual Cloudflare deployment.**

Do not:

* create domains,
* deploy Workers,
* configure Pages,
* configure DNS,
* create production D1 databases,
* set up GitHub deployment workflows,
* add production secrets,
* perform Cloudflare login,
* publish anything.

Cloudflare compatibility is an architectural constraint, not a deployment task.

---

# 6. Monorepo structure

Use a small and obvious structure approximately like:

```text
/
├── frontend/
│   ├── src/
│   ├── public/
│   ├── package.json
│   ├── pnpm-lock.yaml
│   └── astro.config.mjs
│
├── backend/
│   ├── app/
│   ├── scripts/
│   ├── tests/
│   ├── pyproject.toml
│   └── uv.lock
│
├── dataset/
│   ├── ...
│   └── ...
│
├── migrations/
│   └── 001_initial.sql
│
├── README.md
└── ...
```

You may adjust this slightly if there is a clear practical reason.

Do not over-engineer the repository structure.

Someone unfamiliar with the project should be able to understand it quickly.

---

# 7. Database model

Keep the initial schema relational and small.

At minimum:

## songs

Fields approximately:

```text
id
title
artist
album
release_year
popularity_score
listener_count
listen_count
musicbrainz_id
apple_track_id
preview_url
artwork_url
enabled
```

Use sensible SQLite types and constraints.

`album`, MusicBrainz data, artwork, and raw popularity metrics may be nullable where appropriate.

`preview_url` is required for playable songs.

`popularity_score` should use a consistent range, preferably `0–100`.

## genres

```text
id
name
```

Genre names should be normalized.

## song_genres

```text
song_id
genre_id
```

A song can belong to multiple genres.

Add only the indexes that are useful for the actual MVP queries.

---

# 8. Dataset-building component

The song catalog is a key part of the project.

Create a separate Python dataset-building area.

Its eventual purpose is to construct our playable catalog from:

```text
ListenBrainz
    ↓
candidate discovery + popularity

MusicBrainz
    ↓
canonical metadata, identity, release information, genres

Apple/iTunes Search API
    ↓
matching track, artwork, preview URL

    ↓
validated song catalog
```

The runtime web application should **not** query ListenBrainz or MusicBrainz during gameplay.

These are offline ingestion dependencies.

Apple should also be queried during catalog construction to establish that a valid preview exists.

Gameplay uses the stored Apple preview URL.

### Dataset pipeline responsibilities

Structure the code so it can eventually:

1. discover candidate recordings,
2. retrieve popularity metrics,
3. enrich MusicBrainz metadata,
4. normalize genres,
5. match songs against Apple's catalog,
6. reject songs without a confident Apple match,
7. reject songs without a preview,
8. calculate a normalized popularity score,
9. insert/update the local SQL database.

Use MusicBrainz recording IDs when available as the main external canonical identifier.

### Important

Do not spend excessive effort trying to immediately populate thousands of real songs if external API access, rate limits, credentials, or environment restrictions make that impractical.

Instead:

* implement the ingestion structure cleanly,
* implement real API clients where practical,
* make steps resumable/idempotent where reasonable,
* document required environment variables,
* provide a small seed/sample dataset so the application works immediately.

The end-to-end web application must be runnable without waiting for a huge catalog build.

Include enough realistic development data to meaningfully test:

* multiple genres,
* multiple decades,
* different popularity ranges,
* song search,
* random selection.

Do not fabricate Apple URLs that pretend to work. If actual preview URLs cannot safely be provided in seed data, make that limitation explicit and provide a clean path for importing real catalog entries.

---

# 9. Backend API

The backend should remain stateless.

There is no game/session table.

The browser owns the current gameplay state.

Implement a minimal API.

## `POST /api/round`

Accept something similar to:

```json
{
  "genres": ["rock", "alternative rock"],
  "year_min": 1990,
  "year_max": 2010,
  "popularity_min": 60,
  "popularity_max": 100,
  "exclude_ids": [123, 456]
}
```

Find all eligible songs matching:

* enabled,
* requested genre logic,
* year range,
* popularity range,
* not excluded.

For multiple selected genres, treat them as **OR**:

> rock OR alternative rock.

Choose uniformly at random.

Return only what the frontend needs before the answer is revealed.

For example:

```json
{
  "song_id": 9283,
  "preview_url": "..."
}
```

Do not accidentally leak the answer in the round response.

Handle the case where no song matches the filters gracefully.

Do not optimize prematurely.

For the initial catalog size, a straightforward SQLite-compatible random query is acceptable.

---

## `GET /api/songs/search?q=...`

Search candidate guesses.

Return a small bounded result set.

Each result should contain enough information to distinguish songs:

```json
{
  "id": 123,
  "title": "Everlong",
  "artist": "Foo Fighters"
}
```

Make search case-insensitive.

A user must select one of these known songs rather than submit arbitrary free text.

This means correctness can simply be determined by song ID.

Search should feel responsive.

---

## `GET /api/songs/{id}`

Return reveal information:

```json
{
  "id": 9283,
  "title": "Everlong",
  "artist": "Foo Fighters",
  "album": "The Colour and the Shape",
  "release_year": 1997,
  "artwork_url": "...",
  "genres": ["rock", "alternative rock"],
  "preview_url": "..."
}
```

The `preview_url` may be included here because this endpoint is only used after reveal and powers the **Play Full Preview** interaction.

---

## Metadata/filter endpoint

If useful, add one small endpoint such as:

```text
GET /api/filters
```

which can return:

* supported genres,
* minimum available year,
* maximum available year,
* popularity bounds.

Prefer deriving filter options from the catalog rather than duplicating them in frontend constants when practical.

Do not create a large REST API.

---

# 10. Game state

Keep transient game state in React.

Something approximately like:

```text
filters

currentSongId
previewUrl

currentAttempt
currentUnlockedDuration
playbackCursor

previousGuesses
roundStatus
revealedSong

excludedSongIds
```

`roundStatus` should distinguish at least:

```text
playing
correct
failed
gave_up
```

Refresh can reset the current game.

That is acceptable.

Do not introduce server-side sessions or browser persistence unless it is required for basic functionality.

---

# 11. Guessing behavior

The guess workflow should be:

```text
user types
    ↓
debounced search
    ↓
selects a result
    ↓
presses Guess
```

If:

```text
selectedSongId == currentSongId
```

the round is correct.

Otherwise:

* add that guess to previous guesses,
* advance to the next snippet duration,
* preserve the audio cursor at the end of the previously unlocked section.

Prevent submitting the same wrong guess repeatedly.

### Skip

Skip should:

* consume the current attempt,
* advance to the next snippet duration,
* not add a fake guess,
* leave the playback cursor at the end of the previously heard section.

Therefore, after skipping, the next Play should normally begin where the previous snippet ended.

### Give Up / Reveal Song

The Give Up / Reveal Song action should always be visible during an active round.

Pressing it should:

* stop any active audio,
* immediately end the round,
* set the round status to `gave_up`,
* fetch/show the reveal information,
* expose the full-preview playback controls.

It should not require exhausting the attempts first.

At the final attempt, an incorrect guess or skip ends the round and reveals the answer.

---

# 12. Audio behavior

Use a normal HTML audio element controlled from React.

Snippet boundaries:

```ts
[1, 2, 4, 7, 11, 15]
```

There is only one preview URL.

## Continuous playback model

The normal Play action should resume from the current playback cursor rather than automatically restarting at zero.

Example:

```text
stage boundary = 4s
cursor = 2s

Play
→ start at 2s
→ stop at 4s
→ cursor becomes 4s
```

When the next stage is unlocked:

```text
new stage boundary = 7s
cursor remains 4s
```

then:

```text
Play
→ start at 4s
→ stop at 7s
```

## Rewind

Rewind should:

```text
cursor = 0
audio.currentTime = 0
```

It does **not** reduce the currently unlocked duration.

For example:

```text
unlocked duration = 7s
Rewind
Play
→ play 0s → 7s
```

After that playback reaches 7 seconds, the cursor is once again at 7 seconds.

If practical and intuitive, allow the Play control to restart the currently unlocked portion once the cursor has already reached the current boundary, rather than becoming permanently inert.

Keep this behavior simple and predictable.

## Playback controls

Handle:

* Play,
* Pause if useful,
* Rewind,
* repeated Play presses,
* active timers,
* switching to the next round,
* giving up while audio is playing,
* component cleanup.

Do not allow stale timers from an old round to stop audio in a new round.

Handle audio loading and playback errors visibly rather than silently failing.

Do not implement custom audio processing unless necessary.

---

# 13. Full preview after reveal

Once a round is revealed for any reason:

```text
correct
failed
gave_up
```

the gameplay snippet restrictions no longer apply.

Show a **Play Full Preview** control.

This should use Apple's complete available preview URL.

It should allow normal playback of the entire preview rather than stopping at:

```text
1 / 2 / 4 / 7 / 11 / 15 seconds
```

At minimum support:

* Play,
* Pause,
* replay from the beginning.

It may reuse the same underlying `<audio>` element if that keeps the implementation simpler.

When proceeding to Next Song:

* stop playback,
* reset the audio element,
* reset playback state,
* begin the next round cleanly.

---

# 14. Frontend UX

The experience should be:

* minimal,
* fast,
* responsive,
* pleasant on both mobile and desktop.

Design **mobile first**.

Use one centered game column on desktop rather than creating separate desktop/mobile experiences.

Something around:

```css
width: min(calc(100% - 2rem), 36rem);
margin-inline: auto;
```

is an appropriate general layout.

Touch targets should be comfortable on mobile.

Avoid clutter.

The primary Play action should be visually dominant.

Secondary controls such as Rewind and Skip should be clearly available but subordinate.

**Give Up / Reveal Song** should always be discoverable, while visually communicating that it ends the current round.

---

# 15. Suggested frontend component hierarchy

Keep componentization sensible but modest.

Approximately:

```text
Game
│
├── SetupScreen
│   ├── GenreSelector
│   ├── YearRange
│   ├── PopularityRange
│   └── PlayButton
│
└── PlayScreen
    ├── SnippetProgress
    ├── AudioControls
    │   ├── Play
    │   └── Rewind
    ├── GuessSearch
    ├── PreviousGuesses
    ├── SkipButton
    ├── GiveUpButton
    └── RevealCard
        ├── Artwork/details
        ├── FullPreviewPlayer
        └── NextSongButton
```

You do not need to follow this literally if a slightly different structure is cleaner.

Avoid components that contain only a trivial wrapper unless they improve clarity.

---

# 16. Visual direction

Create a polished but restrained interface.

The product is a music game, so it should feel more deliberate than a raw developer demo, but do not spend time building an elaborate design system.

Use:

* strong typography,
* generous spacing,
* subtle surfaces/borders,
* clear hierarchy,
* one accent color,
* clear success/error states,
* simple transitions where they materially improve feedback.

Use CSS variables for basic design tokens.

Do not add visual dependencies just for styling.

Avoid looking like a generic admin dashboard.

The primary action—playing the snippet—should be visually dominant.

The search/guess interaction should be obvious.

On mobile, avoid elements jumping around unnecessarily when autocomplete results or previous guesses appear.

---

# 17. Local development

Provide a straightforward local development workflow.

Use:

```text
backend → uv
frontend → pnpm
```

The frontend and backend may run separately during development.

Configure the Astro development server so frontend code can continue using:

```ts
fetch("/api/...")
```

while proxying `/api` to the locally running FastAPI service.

Use a local SQLite database.

Provide clear commands in the README for:

* installing Python dependencies with `uv`,
* installing frontend dependencies with `pnpm`,
* creating/migrating the local database,
* loading seed data,
* running FastAPI,
* running Astro,
* running tests,
* optionally running the dataset pipeline.

Prefer commands along the lines of:

```bash
cd backend
uv sync
uv run ...
```

and:

```bash
cd frontend
pnpm install
pnpm dev
```

Use lockfiles and commit them.

---

# 18. Configuration

Use environment variables where appropriate for external dataset APIs.

Provide an example env file such as:

```text
.env.example
```

Do not commit secrets.

Do not add production Cloudflare credentials.

Runtime gameplay should ideally require no external API credentials once the catalog exists.

---

# 19. Testing

Add a small but useful test suite.

Backend tests should cover at least:

* round selection respects year filters,
* round selection respects genre filters,
* round selection respects popularity filters,
* excluded IDs are excluded,
* no-match filters return a sensible response,
* search works,
* reveal endpoint works,
* round response does not expose answer metadata.

Frontend/game-state tests should cover the important playback behavior where practical:

* wrong guess advances the snippet boundary,
* Skip advances the snippet boundary,
* Skip does not add a fake guess,
* playback resumes from the previous boundary,
* Rewind resets playback to zero without changing the unlocked boundary,
* Give Up immediately reveals the answer,
* correct guess reveals the answer,
* final failed attempt reveals the answer,
* revealed state allows unrestricted full-preview playback,
* Next Song resets playback state.

Frontend tests do not need to be extensive beyond the important state transitions.

Do not build a large testing infrastructure.

---

# 20. Code quality

Use:

* type hints in Python,
* TypeScript types,
* small cohesive modules,
* clear names,
* explicit error handling,
* formatting/linting tools where lightweight.

Avoid:

* unnecessary dependency injection frameworks,
* repositories/services/interfaces for every table,
* premature generic abstractions,
* huge utility modules,
* excessive comments explaining obvious code,
* speculative architecture for future features.

Prefer readable straightforward code.

---

# 21. Implementation order

Work bottom-up.

## Phase 1 — Foundation

Create:

* monorepo structure,
* Python project managed with `uv`,
* Astro/React project managed with `pnpm`,
* SQL migration,
* local SQLite initialization,
* sample seed catalog.

Verify the database can represent songs and multiple genres.

## Phase 2 — Backend

Implement:

* database access,
* `/api/round`,
* `/api/songs/search`,
* `/api/songs/{id}`,
* filter metadata if useful.

Test the API independently.

At this point we should be able to answer:

> Give me a random playable rock song from 1990–2010 with popularity 60–100.

## Phase 3 — Minimal playable frontend

Build the React game island.

Initially make an end-to-end round work:

```text
request song
→ play first section
→ search
→ guess or skip
→ unlock more audio
→ continue playback from previous boundary
→ optionally rewind
→ correct / fail / give up
→ reveal
→ play full preview
→ next
```

Do not polish prematurely.

## Phase 4 — Filters

Connect:

* genres,
* year range,
* popularity range.

Make filter state produce the expected backend queries.

Handle impossible filter combinations gracefully.

## Phase 5 — UX/polish

Make the application pleasant on:

* narrow mobile screens,
* normal desktop screens.

Improve:

* loading states,
* autocomplete,
* audio feedback,
* playback progression,
* rewind interaction,
* incorrect guesses,
* give-up/reveal interaction,
* full-preview player,
* reveal transitions,
* empty/error states.

## Phase 6 — Dataset tooling

Complete the initial ingestion modules for:

* ListenBrainz,
* MusicBrainz,
* Apple.

Make the pipeline understandable and documented.

Do not block completion of the playable MVP on creating a giant production catalog.

---

# 22. Definition of done

The task is complete when:

1. The repository contains the frontend, backend, database schema, dataset tooling, and documentation.

2. A developer can clone the repository, follow the README, initialize the local database, run backend and frontend, and open the application.

3. The user can:

   * choose genres,
   * choose a year range,
   * choose a popularity range,
   * start playing,
   * hear progressively longer portions of the song,
   * skip forward without being forced to rehear previous audio,
   * rewind and intentionally hear the unlocked portion again,
   * search the song catalog,
   * submit guesses,
   * give up/reveal at any point,
   * reveal the song,
   * play the complete Apple preview after reveal,
   * continue indefinitely with Next Song.

4. Song selection is performed by our own SQL catalog, not by querying Apple dynamically for arbitrary search terms during gameplay.

5. Songs can belong to multiple genres.

6. The backend does not leak the answer before reveal.

7. The UI works well on both mobile and desktop.

8. There are no accounts or multiplayer features.

9. The SQL remains SQLite/D1-compatible.

10. The frontend uses relative `/api/...` URLs and is structurally compatible with eventually serving frontend and API from the same Cloudflare origin.

11. The backend uses `uv`.

12. The frontend uses `pnpm`.

13. The codebase is in a sensible shape to later adapt to:

    * Cloudflare static assets,
    * Python Workers/FastAPI,
    * D1.

14. **Nothing has actually been deployed or configured on Cloudflare.**

---

# 23. Working style

Implement the project rather than only describing it.

Inspect your work as you go.

Run the relevant:

* builds,
* type checks,
* backend tests,
* frontend checks,
* local database setup.

Fix issues you encounter.

Make reasonable decisions independently when details are unspecified.

If there is tension between a sophisticated architecture and a simpler implementation that satisfies this MVP, **choose the simpler implementation**.

At the end, provide a concise summary covering:

* final repository structure,
* major implementation decisions,
* what is fully working,
* dataset ingestion status,
* commands to run the project locally,
* tests/checks run and their results,
* any known limitations,
* what remains specifically for a future Cloudflare deployment.

Do not deploy anything.

