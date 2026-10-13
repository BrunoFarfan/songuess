import test from "node:test";
import assert from "node:assert/strict";
import { DatabaseSync } from "node:sqlite";
import { readFileSync, readdirSync } from "node:fs";
import {
  getFilters,
  getContextFilters,
  searchSongs,
  searchArtists,
  chooseRound,
  revealSong,
  browseIndex,
  normalizeSearch,
} from "../src/catalog-worker.mjs";
const migrationRoot = new URL("../../migrations/", import.meta.url);
function fixture() {
  const db = new DatabaseSync(":memory:");
  for (const file of readdirSync(migrationRoot).sort())
    db.exec(readFileSync(new URL(file, migrationRoot), "utf8"));
  const songs = [
    [1, "Halo", "Beyoncé", "I Am", 2008, 95],
    [2, "Hello", "Adele", "25", 2015, 90],
    [3, "Hello Again", "Neil Diamond", "Jazz", 1980, 50],
    [4, "Other", "Hello", "Hello", 1990, 60],
    [5, "Héllo", "Guest", "Unknown", 2000, 20],
    [6, "Hidden", "Nobody", "No", 2000, 10],
  ];
  for (const [id, title, artist, album, year, score] of songs)
    db.prepare(
      "INSERT INTO songs(id,title,artist,album,release_year,popularity_score,preview_url,enabled) VALUES(?,?,?,?,?,?,?,?)",
    ).run(
      id,
      title,
      artist,
      album,
      year,
      score,
      `https://preview/${id}`,
      id === 6 ? 0 : 1,
    );
  db.exec(
    "INSERT INTO genres VALUES (1,'pop'),(2,'rock'); INSERT INTO countries VALUES(1,'US'),(2,'GB'); INSERT INTO artists(id,musicbrainz_id,name) VALUES (1,'adele','Adele'),(2,'beyonce','Beyoncé'); INSERT INTO song_genres VALUES(1,1),(2,1),(3,2),(4,2),(5,1); INSERT INTO song_countries VALUES(1,1),(2,2),(3,1),(4,1),(5,2); INSERT INTO song_artists VALUES(1,2,0,'Beyoncé',''),(2,1,0,'Adele',''),(5,1,0,'Guest',''); INSERT INTO song_genre_evidence VALUES(1,1,1,100,'apple')",
  );
  for (const [id, title, artist, album, year] of songs.filter(
    (s) => s[0] !== 6,
  )) {
    const fields = [title, artist, album].map(normalizeSearch),
      compact = fields.map((f) => f.replaceAll(" ", "")),
      text = [...fields, String(year), ...compact].join(" ");
    db.prepare("INSERT INTO song_search VALUES(?,?,?,?,?,?,?,?,?)").run(
      id,
      fields[0],
      compact[0],
      fields[1],
      compact[1],
      fields[2],
      compact[2],
      String(year),
      text,
    );
    db.prepare("INSERT INTO song_search_fts VALUES(?,?)").run(id, text);
  }
  for (const [id, name] of [
    [1, "Adele"],
    [1, "Guest"],
    [2, "Beyoncé"],
  ]) {
    const normalized = normalizeSearch(name),
      compact = normalized.replaceAll(" ", "");
    db.prepare("INSERT INTO artist_search_aliases VALUES(?,?,?,?)").run(
      id,
      name,
      normalized,
      compact,
    );
    db.prepare("INSERT INTO artist_search_fts VALUES(?,?)").run(
      id,
      `${normalized} ${compact}`,
    );
  }
  db.exec(
    readFileSync(new URL("014_runtime_catalog.sql", migrationRoot), "utf8"),
  );
  const queries = [];
  const env = {
    DB: {
      prepare(sql) {
        return {
          bind(...args) {
            queries.push({ sql, args });
            return {
              async all() {
                return { results: db.prepare(sql).all(...args) };
              },
              async first() {
                return db.prepare(sql).get(...args) ?? null;
              },
            };
          },
        };
      },
    },
  };
  return { env, db, queries };
}
const params = (values) => new URLSearchParams(values);
test("snapshot metadata and progressive facets apply only preceding selections", async () => {
  const { env } = fixture();
  assert.deepEqual(await getFilters(env), {
    genres: ["pop", "rock"],
    countries: ["GB", "US"],
    year_min: 1980,
    year_max: 2015,
    popularity_min: 0,
    popularity_max: 100,
    song_count: 5,
  });
  assert.deepEqual(
    await getContextFilters(env, {
      genres: [" POP "],
      countries: ["gb"],
      year_min: 2010,
      popularity_min: 91,
    }),
    {
      genres: ["pop", "rock"],
      countries: ["GB", "US"],
      year_min: 2000,
      year_max: 2015,
      popularity_min: 90,
      popularity_max: 90,
      song_count: 0,
    },
  );
  assert.deepEqual(
    (await getContextFilters(env, { artist_id: "beyonce" })).genres,
    ["pop"],
  );
});
test("search ranking, ASCII normalization, short prefixes and bounded FTS candidates", async () => {
  const { env, queries } = fixture();
  const result = await searchSongs(env, params({ q: "Héllo", limit: "2" }));
  assert.deepEqual(
    result.items.map((r) => r.id),
    [2, 5],
  );
  assert.equal(result.has_more, true);
  assert.equal(
    (await searchSongs(env, params({ q: "hello", offset: "2" }))).items[0].id,
    4,
  );
  assert.deepEqual(
    (await searchSongs(env, params({ q: "be" }))).items.map((r) => r.id),
    [1],
  );
  assert.ok(queries.find((q) => q.sql.includes("ORDER BY f.rank LIMIT ?")));
  assert.equal(normalizeSearch("Beyoncé & JAY-Z"), "beyonce jay z");
});
test("browse permutation is seeded, bijective and pages never overlap", async () => {
  for (const size of [1, 2, 3, 5, 17, 10617]) {
    const indices = Array.from({ length: size }, (_, i) =>
      browseIndex(i, size, 4294967295),
    );
    assert.equal(new Set(indices).size, size);
    assert.ok(indices.every((i) => i >= 0 && i < size));
  }
  const { env } = fixture();
  const a = await searchSongs(env, params({ seed: "42", limit: "2" })),
    b = await searchSongs(env, params({ seed: "42", limit: "2", offset: "2" }));
  assert.deepEqual(
    await searchSongs(env, params({ seed: "42", limit: "2" })),
    a,
  );
  assert.equal(new Set([...a.items, ...b.items].map((r) => r.id)).size, 4);
});
test("credited artist names match structured IDs and unique song counts", async () => {
  const { env } = fixture();
  assert.deepEqual(await searchArtists(env, params({ q: "guest" })), [
    { id: "adele", name: "Guest", disambiguation: null, song_count: 2 },
  ]);
  assert.equal(
    (await searchArtists(env, params({ q: "Beyonce" })))[0].id,
    "beyonce",
  );
});
test("round filters, exclusions and live validation protect stale snapshots", async () => {
  const { env, db } = fixture(),
    request = {
      genres: [" POP "],
      countries: ["gb"],
      artist_id: "adele",
      year_min: 2000,
      year_max: 2200,
      popularity_min: 0,
      popularity_max: 100,
      exclude_ids: [5],
    };
  assert.deepEqual(await chooseRound(env, request), {
    song_id: 2,
    preview_url: "https://preview/2",
  });
  db.exec("UPDATE songs SET enabled=0 WHERE id=2");
  assert.equal(await chooseRound(env, request), null);
  assert.equal(
    await chooseRound(env, { ...request, exclude_ids: [2, 5] }),
    null,
  );
});
test("song reveal includes evidence ordered genres and streaming fields", async () => {
  const { env } = fixture(),
    song = await revealSong(env, 1);
  assert.deepEqual(song.genres, ["pop"]);
  assert.equal(song.spotify_url, null);
  assert.equal(song.title, "Halo");
  assert.equal(await revealSong(env, 6), null);
});
test("invalid filters and pagination fail with HTTP 422", async () => {
  const { env } = fixture();
  for (const body of [
    { countries: ["USA"] },
    { year_min: 2000, year_max: 1900 },
    { genres: "pop" },
    { extra: 1 },
  ])
    await assert.rejects(getContextFilters(env, body), { status: 422 });
  await assert.rejects(chooseRound(env, {}), { status: 422 });
  for (const values of [
    { limit: "0" },
    { seed: "4294967296" },
    { offset: "-1" },
    { limit: "1.5" },
    { q: "a".repeat(121) },
  ])
    await assert.rejects(searchSongs(env, params(values)), { status: 422 });
});
test("concurrent cold requests execute their own I/O and cache only completed results", async () => {
  let calls = 0,
    releases = [];
  const data = {
    songs: [],
    genres: [],
    countries: [],
    artists: [],
    song_genres: [],
    song_countries: [],
    song_artists: [],
    browse_count: 0,
  };
  const env = {
    DB: {
      prepare() {
        return {
          bind() {
            return {
              first() {
                calls++;
                return new Promise((resolve) =>
                  releases.push(() =>
                    resolve({
                      genres: "[]",
                      countries: "[]",
                      year_min: null,
                      year_max: null,
                      song_count: 0,
                    }),
                  ),
                );
              },
            };
          },
        };
      },
    },
  };
  const a = getFilters(env),
    b = getFilters(env);
  assert.equal(calls, 2);
  releases.forEach((r) => r());
  await Promise.all([a, b]);
  await getFilters(env);
  assert.equal(calls, 2);
});
