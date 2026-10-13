import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { DatabaseSync } from "node:sqlite";
import { test } from "node:test";
import {
  handleRoomRequest,
  cleanupExpiredRooms,
} from "../src/rooms-worker.mjs";

// Execute actual SQL through the same asynchronous prepared-statement surface as D1.
function fixture(count = 4) {
  const sqlite = new DatabaseSync(":memory:");
  sqlite.exec(`PRAGMA foreign_keys = ON;
    CREATE TABLE songs (id INTEGER PRIMARY KEY, title TEXT, artist TEXT, album TEXT, release_year INTEGER, artwork_url TEXT, popularity_score INTEGER, preview_url TEXT, apple_track_id TEXT, apple_music_url TEXT, spotify_url TEXT, enabled INTEGER DEFAULT 1);
    CREATE TABLE genres (id INTEGER PRIMARY KEY, name TEXT);
    CREATE TABLE song_genres (song_id INTEGER, genre_id INTEGER);
    CREATE TABLE countries (id INTEGER PRIMARY KEY, code TEXT);
    CREATE TABLE song_countries (song_id INTEGER, country_id INTEGER);
    CREATE TABLE artists (id INTEGER PRIMARY KEY, musicbrainz_id TEXT);
    CREATE TABLE song_artists (song_id INTEGER, artist_id INTEGER);`);
  sqlite.exec(
    readFileSync(
      new URL("../../migrations/015_challenge_rooms.sql", import.meta.url),
      "utf8",
    ),
  );
  for (let id = 1; id <= count; id++)
    sqlite
      .prepare(
        "INSERT INTO songs(id,title,artist,release_year,popularity_score,preview_url,apple_track_id) VALUES (?,?,'Artist',2000,80,?,?)",
      )
      .run(id, `Song ${id}`, `https://audio/${id}`, `apple-${id}`);
  let beforeRun;
  const DB = {
    prepare(sql) {
      let params = [];
      const statement = {
        bind(...values) {
          params = values;
          return statement;
        },
        async first() {
          return sqlite.prepare(sql).get(...params) ?? null;
        },
        async all() {
          return { results: sqlite.prepare(sql).all(...params) };
        },
        async run() {
          beforeRun?.(sql);
          const result = sqlite.prepare(sql).run(...params);
          return { meta: { changes: Number(result.changes) } };
        },
      };
      return statement;
    },
    async batch(statements) {
      sqlite.exec("BEGIN");
      try {
        const result = [];
        for (const statement of statements) result.push(await statement.run());
        sqlite.exec("COMMIT");
        return result;
      } catch (error) {
        sqlite.exec("ROLLBACK");
        throw error;
      }
    },
  };
  const env = {
    DB,
    SONGUESS_GUEST_SECRET: "test-secret-with-at-least-32-characters",
  };
  function client() {
    let cookie;
    return async (path, body, options = {}) => {
      const headers = { ...(cookie ? { cookie } : {}), ...options.headers };
      const request = new Request(`https://songuess.test${path}`, {
        method: options.method || (body !== undefined ? "POST" : "GET"),
        headers,
        ...(body !== undefined ? { body: JSON.stringify(body) } : {}),
      });
      const response = await handleRoomRequest(request, env);
      if (!response) return null;
      if (response.headers.get("set-cookie"))
        cookie = response.headers.get("set-cookie").split(";")[0];
      return {
        status: response.status,
        data: await response.json(),
        headers: response.headers,
        cookie,
      };
    };
  }
  return {
    sqlite,
    env,
    client,
    beforeRun: (hook) => {
      beforeRun = hook;
    },
  };
}
const filters = {
  year_min: 1900,
  year_max: 2100,
  popularity_min: 0,
  popularity_max: 100,
};
async function setup(t, count = 4) {
  const f = fixture(count);
  t.after(() => f.sqlite.close());
  const c = f.client();
  const session = await c("/api/guest-session", {});
  const created = await c("/api/rooms", {
    display_name: "  Player  ",
    song_count: count,
    filters,
  });
  assert.equal(created.status, 200);
  const code = created.data.code;
  const me = () => c(`/api/rooms/${code}/me`);
  const action = async (action, song_id, overrides = {}) => {
    const current = (await me()).data.current_round;
    return c(`/api/rooms/${code}/actions`, {
      action,
      position: current?.position ?? 1,
      expected_version: current?.version ?? 0,
      ...(song_id ? { song_id } : {}),
      ...overrides,
    });
  };
  const answer = (position) =>
    JSON.parse(
      f.sqlite
        .prepare(
          "SELECT reveal_json FROM room_songs WHERE room_code=? AND position=?",
        )
        .get(code, position).reveal_json,
    ).id;
  return { ...f, c, code, me, action, answer, session };
}

test("native rooms freeze playlist, keep summary anonymous, and score a resumed challenge", async (t) => {
  const { c, code, me, action, answer, session } = await setup(t);
  assert.equal(
    (await c("/api/guest-session", {})).data.guest_id,
    session.data.guest_id,
  );
  const initial = (await me()).data;
  assert.equal(initial.display_name, "Player");
  assert.equal(initial.playlist.length, 4);
  assert.equal(
    initial.current_round.preview_url,
    initial.playlist[0].reveal.preview_url,
  );
  assert.equal(initial.current_round.song_id, undefined);
  assert.equal(initial.current_round.reveal, undefined);
  const first = await action("guess", answer(1));
  assert.equal(first.status, 200);
  assert.equal(first.data.round.result, "clue_1");
  assert.equal(first.data.state.total_points, 6);
  assert.equal((await action("skip")).data.round.clue_number, 2);
  assert.equal((await action("guess", answer(2))).data.state.total_points, 11);
  const summary = (await c(`/api/rooms/${code}`)).data;
  assert.equal(summary.playlist, undefined);
  assert.equal(summary.leaderboard[0].player_id.length, 16);
  assert.notEqual(summary.leaderboard[0].player_id, session.data.guest_id);
  assert.deepEqual(
    summary.leaderboard[0].songs.map((song) => song.points),
    [6, 5, null, null],
  );
  assert.deepEqual((await me()).data.playlist, initial.playlist);
});

test("CAS consumes one clue, duplicate guesses use Apple identity, and frozen answers survive replacement", async (t) => {
  const { sqlite, c, code, me, action, answer } = await setup(t);
  const wrong = [1, 2, 3, 4].find((id) => id !== answer(1));
  const first = await action("guess", wrong);
  assert.equal(first.data.round.result, null);
  assert.equal(first.data.round.guesses[0]._apple_track_id, undefined);
  assert.equal(
    (await action("guess", wrong)).data.detail,
    "You already guessed this song",
  );
  assert.equal(
    (await action("skip", null, { expected_version: 0 })).status,
    409,
  );
  assert.equal((await action("skip", null, { position: 2 })).status, 409);
  const current = (await me()).data.current_round;
  const responses = await Promise.all(
    [1, 2].map(() =>
      c(`/api/rooms/${code}/actions`, {
        action: "skip",
        position: 1,
        expected_version: current.version,
      }),
    ),
  );
  assert.deepEqual(
    responses.map((response) => response.status).sort(),
    [200, 409],
  );
  const frozen = (await me()).data.playlist[0].reveal;
  const identity = sqlite
    .prepare("SELECT apple_track_id FROM songs WHERE id=?")
    .get(frozen.id).apple_track_id;
  sqlite.exec("DELETE FROM songs");
  sqlite
    .prepare(
      "INSERT INTO songs(id,title,artist,release_year,popularity_score,preview_url,apple_track_id) VALUES (99,'Updated','Artist',2000,80,'https://new',?)",
    )
    .run(identity);
  const solved = await action("guess", 99);
  assert.equal(solved.data.round.result, "clue_3");
  assert.deepEqual(solved.data.round.reveal, frozen);
  assert.equal(solved.data.state.total_points, 4);
});

test("join is idempotent, authorization and origin checks reject invalid access", async (t) => {
  const { env, c, code, client, session } = await setup(t);
  const outsider = client();
  assert.equal((await outsider(`/api/rooms/${code}/me`)).status, 401);
  assert.equal((await outsider("/api/guest-session", {})).status, 200);
  assert.equal((await outsider(`/api/rooms/${code}/me`)).status, 403);
  assert.equal(
    (
      await outsider(`/api/rooms/${code}/actions`, {
        position: 1,
        expected_version: 0,
        action: "skip",
      })
    ).status,
    403,
  );
  assert.equal(
    (await outsider(`/api/rooms/${code}/join`, { display_name: "Guest" }))
      .status,
    200,
  );
  assert.equal(
    (await outsider(`/api/rooms/${code}/join`, { display_name: "Renamed" }))
      .data.display_name,
    "Guest",
  );
  assert.equal(
    (
      await c(
        "/api/guest-session",
        {},
        { headers: { "sec-fetch-site": "cross-site" } },
      )
    ).status,
    403,
  );
  assert.equal(
    (
      await c(
        `/api/rooms/${code}/join`,
        { display_name: "x" },
        { headers: { origin: "https://evil.test" } },
      )
    ).status,
    403,
  );
  const token = session.cookie;
  const forged = token.slice(0, -1) + (token.endsWith("a") ? "b" : "a");
  assert.equal(
    (
      await c(`/api/rooms/${code}/me`, undefined, {
        headers: { cookie: forged },
      })
    ).status,
    401,
  );
  assert.equal(
    (
      await c(`/api/rooms/${code}/me`, undefined, {
        headers: { cookie: token.split(".")[0] + ".0." + token.split(".")[2] },
      })
    ).status,
    401,
  );
  const response = await handleRoomRequest(
    new Request("https://songuess.test/api/guest-session", { method: "POST" }),
    { ...env, SONGUESS_GUEST_SECRET: "short" },
  );
  assert.equal(response.status, 503);
  assert.equal(
    await handleRoomRequest(
      new Request("https://songuess.test/api/other"),
      env,
    ),
    null,
  );
});

test("closed rooms reveal unsolved results without advancing progress and reject new joins", async (t) => {
  const { sqlite, c, code, me, action, client } = await setup(t);
  sqlite.prepare("UPDATE rooms SET closes_at=0 WHERE code=?").run(code);
  const closed = (await me()).data;
  assert.equal(closed.closed, true);
  assert.equal(closed.closes_at, "1970-01-01T00:00:00Z");
  assert.equal(closed.current_round, null);
  assert.equal(closed.completed_count, 0);
  assert.deepEqual(
    closed.results.map((row) => row.result),
    Array(4).fill("not_guessed"),
  );
  assert.equal((await action("skip")).status, 409);
  const outsider = client();
  await outsider("/api/guest-session", {});
  assert.equal(
    (await outsider(`/api/rooms/${code}/join`, { display_name: "Late" }))
      .status,
    409,
  );
  const summary = (await c(`/api/rooms/${code}`)).data;
  assert.deepEqual(
    summary.leaderboard[0].songs.map((song) => song.points),
    [0, 0, 0, 0],
  );
});

test("deadline crossing cannot commit an action and full rooms reject joins", async (t) => {
  const { sqlite, code, action, beforeRun, client } = await setup(t);
  for (let i = 0; i < 49; i++)
    sqlite
      .prepare("INSERT INTO participants VALUES (?,?,'Guest',0)")
      .run(code, `guest-${i}`);
  const outsider = client();
  await outsider("/api/guest-session", {});
  assert.equal(
    (await outsider(`/api/rooms/${code}/join`, { display_name: "Full" }))
      .status,
    409,
  );
  beforeRun((sql) => {
    if (sql.startsWith("UPDATE participant_rounds"))
      sqlite.prepare("UPDATE rooms SET closes_at=0 WHERE code=?").run(code);
  });
  assert.equal((await action("skip")).status, 409);
  const stored = sqlite
    .prepare(
      "SELECT version FROM participant_rounds WHERE room_code=? AND position=1",
    )
    .get(code);
  assert.equal(stored.version, 0);
});

test("room filters reject exclusions, apply facets, and never persist insufficient selections", async (t) => {
  const { sqlite, c } = await setup(t);
  for (const body of [
    { display_name: " ", filters },
    { display_name: "x", song_count: 21, filters },
    { display_name: "x", filters: { ...filters, exclude_ids: [] } },
    { display_name: "x", filters: { ...filters, year_min: 2200 } },
    { display_name: "x", filters, unknown: true },
  ]) {
    const response = await c("/api/rooms", body);
    assert.equal(response.status, 422);
    assert.ok(Array.isArray(response.data.detail));
  }
  sqlite.exec(
    "INSERT INTO genres VALUES (1,'rock'); INSERT INTO song_genres VALUES (1,1); INSERT INTO countries VALUES (1,'CL'); INSERT INTO song_countries VALUES (1,1); INSERT INTO artists VALUES (1,'artist-one'); INSERT INTO song_artists VALUES (1,1);",
  );
  const filtered = {
    ...filters,
    genres: [" ROCK ", "rock"],
    countries: ["cl"],
    artist_id: "artist-one",
  };
  const created = await c("/api/rooms", {
    display_name: "Filtered",
    song_count: 1,
    filters: filtered,
  });
  assert.equal(created.status, 200);
  assert.equal(
    (await c(`/api/rooms/${created.data.code}/me`)).data.playlist[0].reveal.id,
    1,
  );
  const insufficient = await c("/api/rooms", {
    display_name: "Filtered",
    song_count: 2,
    filters: filtered,
  });
  assert.equal(insufficient.status, 422);
  assert.equal(
    insufficient.data.detail,
    "Not enough songs match these filters",
  );
  assert.equal(sqlite.prepare("SELECT COUNT(*) AS n FROM rooms").get().n, 2);
});

test("six clues finish without guessing; standings tie-break using correct count then histogram", async (t) => {
  const { sqlite, code, action, c, client } = await setup(t);
  for (let i = 0; i < 5; i++)
    assert.equal((await action("skip")).data.round.result, null);
  assert.equal((await action("skip")).data.round.result, "not_guessed");
  const outsider = client();
  await outsider("/api/guest-session", {});
  await outsider(`/api/rooms/${code}/join`, { display_name: "Other" });
  const guests = sqlite
    .prepare(
      "SELECT guest_id FROM participants WHERE room_code=? ORDER BY display_name",
    )
    .all(code);
  const [other, own] = guests.map((row) => row.guest_id);
  sqlite
    .prepare(
      "UPDATE participant_rounds SET result='clue_4' WHERE room_code=? AND guest_id=? AND position <= 2",
    )
    .run(code, other);
  sqlite
    .prepare(
      "UPDATE participant_rounds SET result='clue_1' WHERE room_code=? AND guest_id=? AND position=1",
    )
    .run(code, own);
  let board = (await c(`/api/rooms/${code}`)).data.leaderboard;
  assert.deepEqual(
    board.map((entry) => entry.points),
    [6, 6],
  );
  assert.deepEqual(
    board.map((entry) => entry.correct_count),
    [2, 1],
  );
  sqlite
    .prepare(
      "UPDATE participant_rounds SET result='clue_4' WHERE room_code=? AND guest_id=? AND position=1",
    )
    .run(code, own);
  sqlite
    .prepare(
      "UPDATE participant_rounds SET result='clue_4' WHERE room_code=? AND guest_id=? AND position=2",
    )
    .run(code, own);
  board = (await c(`/api/rooms/${code}`)).data.leaderboard;
  assert.deepEqual(
    board.map((entry) => entry.rank),
    [1, 1],
  );
  sqlite
    .prepare(
      "UPDATE participant_rounds SET result='clue_2' WHERE room_code=? AND guest_id=? AND position <= 2",
    )
    .run(code, other);
  sqlite
    .prepare(
      "UPDATE participant_rounds SET result='clue_1' WHERE room_code=? AND guest_id=? AND position=1",
    )
    .run(code, own);
  sqlite
    .prepare(
      "UPDATE participant_rounds SET result='clue_3' WHERE room_code=? AND guest_id=? AND position=2",
    )
    .run(code, own);
  board = (await c(`/api/rooms/${code}`)).data.leaderboard;
  assert.deepEqual(
    board.map((entry) => entry.points),
    [10, 10],
  );
  assert.deepEqual(
    board.map((entry) => entry.correct_count),
    [2, 2],
  );
  assert.deepEqual(
    board.map((entry) => entry.is_you),
    [true, false],
  );
});

test("cleanup expires in batches and foreign keys cascade every private room record", async (t) => {
  const { sqlite, env, code, c } = await setup(t);
  sqlite
    .prepare("UPDATE rooms SET closes_at=0,delete_at=1 WHERE code=?")
    .run(code);
  assert.equal((await c(`/api/rooms/${code}`)).status, 404);
  assert.equal((await c(`/api/rooms/${code}/me`)).status, 404);
  for (let i = 0; i < 205; i++)
    sqlite
      .prepare("INSERT INTO rooms VALUES (?,'private','{}',1,0,0,1)")
      .run(String(i).padStart(8, "0"));
  assert.equal(await cleanupExpiredRooms(env, 1), 206);
  for (const table of [
    "rooms",
    "participants",
    "room_songs",
    "participant_rounds",
  ])
    assert.equal(
      sqlite.prepare(`SELECT COUNT(*) AS n FROM ${table}`).get().n,
      0,
    );
});
