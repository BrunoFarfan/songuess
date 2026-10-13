/** Native D1 implementation of the anonymous challenge API. */
const COOKIE = "songuess_guest";
const SESSION_AGE = 30 * 86400;
const RETENTION = 7 * 86400;
const encoder = new TextEncoder();
const now = () => Math.floor(Date.now() / 1000);
const timestamp = (value) =>
  value == null
    ? null
    : new Date(value * 1000).toISOString().replace(".000Z", "Z");
const json = (value, status = 200, headers = {}) =>
  new Response(JSON.stringify(value), {
    status,
    headers: { "Content-Type": "application/json", ...headers },
  });
class RoomError extends Error {
  constructor(status, detail) {
    super(typeof detail === "string" ? detail : "Invalid request");
    this.status = status;
    this.detail = detail;
  }
}
const fail = (status, detail) => {
  throw new RoomError(status, detail);
};
const stmt = (env, sql, params = []) => env.DB.prepare(sql).bind(...params);
const first = (env, sql, params) => stmt(env, sql, params).first();
const all = async (env, sql, params) =>
  (await stmt(env, sql, params).all()).results;
const batch = (env, statements) =>
  env.DB.batch(statements.map(([sql, params]) => stmt(env, sql, params)));
const hex = (bytes) =>
  Array.from(new Uint8Array(bytes), (value) =>
    value.toString(16).padStart(2, "0"),
  ).join("");
function secret(env) {
  if (
    !env.SONGUESS_GUEST_SECRET ||
    [...String(env.SONGUESS_GUEST_SECRET)].length < 32
  ) {
    fail(503, "Guest sessions are temporarily unavailable");
  }
  return String(env.SONGUESS_GUEST_SECRET);
}
async function signingKey(env) {
  return crypto.subtle.importKey(
    "raw",
    encoder.encode(secret(env)),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign", "verify"],
  );
}
async function guest(request, env, required = true) {
  const key = await signingKey(env);
  const cookie = (request.headers.get("cookie") || "")
    .split(";")
    .map((item) => item.trim())
    .find((item) => item.startsWith(`${COOKIE}=`));
  const [id, expiry, signature, extra] = (
    cookie?.slice(COOKIE.length + 1) || ""
  ).split(".");
  if (
    extra === undefined &&
    /^[0-9a-f]{32}$/.test(id || "") &&
    /^\d+$/.test(expiry || "") &&
    Number(expiry) > now() &&
    /^[0-9a-f]{64}$/.test(signature || "")
  ) {
    const bytes = Uint8Array.from(signature.match(/../g), (value) =>
      parseInt(value, 16),
    );
    if (
      await crypto.subtle.verify(
        "HMAC",
        key,
        bytes,
        encoder.encode(`${id}.${expiry}`),
      )
    )
      return id;
  }
  if (required) fail(401, "Start a guest session first");
  return null;
}
function origin(request) {
  if (request.headers.get("sec-fetch-site") === "cross-site")
    fail(403, "Cross-site request rejected");
  const value = request.headers.get("origin");
  if (value) {
    try {
      if (new URL(value).origin !== new URL(request.url).origin)
        fail(403, "Cross-site request rejected");
    } catch {
      fail(403, "Cross-site request rejected");
    }
  }
}
function validation(type, loc, msg, input, ctx) {
  const entry = { type, loc: ["body", ...loc], msg, input };
  if (ctx) entry.ctx = ctx;
  fail(422, [entry]);
}
function object(value, loc = []) {
  if (!value || typeof value !== "object" || Array.isArray(value))
    validation(
      "model_attributes_type",
      loc,
      "Input should be a valid dictionary or object to extract fields from",
      value,
    );
}
function extras(value, allowed, loc = []) {
  for (const key of Object.keys(value))
    if (!allowed.includes(key))
      validation(
        "extra_forbidden",
        [...loc, key],
        "Extra inputs are not permitted",
        value[key],
      );
}
function integer(value, key, min, max, loc = [], fallback) {
  if (value === undefined && fallback !== undefined) return fallback;
  if (value === undefined)
    validation("missing", [...loc, key], "Field required", {});
  const parsed =
    typeof value === "boolean"
      ? Number(value)
      : typeof value === "number" ||
          (typeof value === "string" && value.trim() !== "")
        ? Number(value)
        : NaN;
  if (!Number.isInteger(parsed))
    validation(
      "int_parsing",
      [...loc, key],
      "Input should be a valid integer, unable to parse string as an integer",
      value,
    );
  if (min != null && parsed < min)
    validation(
      "greater_than_equal",
      [...loc, key],
      `Input should be greater than or equal to ${min}`,
      value,
      { ge: min },
    );
  if (max != null && parsed > max)
    validation(
      "less_than_equal",
      [...loc, key],
      `Input should be less than or equal to ${max}`,
      value,
      { le: max },
    );
  return parsed;
}
function name(body) {
  if (typeof body.display_name !== "string")
    validation(
      body.display_name === undefined ? "missing" : "string_type",
      ["display_name"],
      body.display_name === undefined
        ? "Field required"
        : "Input should be a valid string",
      body.display_name ?? body,
    );
  const value = body.display_name.trim();
  if ([...value].length < 1)
    validation(
      "string_too_short",
      ["display_name"],
      "String should have at least 1 character",
      body.display_name,
      { min_length: 1 },
    );
  if ([...value].length > 24)
    validation(
      "string_too_long",
      ["display_name"],
      "String should have at most 24 characters",
      body.display_name,
      { max_length: 24 },
    );
  return value;
}
function filters(input) {
  object(input, ["filters"]);
  if ("exclude_ids" in input)
    validation(
      "value_error",
      ["filters"],
      "Value error, Room filters do not accept exclude_ids",
      input,
      { error: {} },
    );
  extras(
    input,
    [
      "genres",
      "countries",
      "artist_id",
      "year_min",
      "year_max",
      "popularity_min",
      "popularity_max",
    ],
    ["filters"],
  );
  const result = { genres: [], countries: [], artist_id: null };
  for (const key of ["genres", "countries"]) {
    const values = input[key] === undefined ? [] : input[key];
    if (!Array.isArray(values))
      validation(
        "list_type",
        ["filters", key],
        "Input should be a valid list",
        values,
      );
    if (values.length > 30)
      validation(
        "too_long",
        ["filters", key],
        "List should have at most 30 items after validation, not more",
        values,
        { field_type: "List", max_length: 30, actual_length: values.length },
      );
    values.forEach((value, i) => {
      if (typeof value !== "string")
        validation(
          "string_type",
          ["filters", key, i],
          "Input should be a valid string",
          value,
        );
      if (key === "countries" && !/^[A-Za-z]{2}$/.test(value))
        validation(
          "string_pattern_mismatch",
          ["filters", key, i],
          "String should match pattern '^[A-Za-z]{2}$'",
          value,
          { pattern: "^[A-Za-z]{2}$" },
        );
    });
    result[key] = values;
  }
  if (input.artist_id != null) {
    if (typeof input.artist_id !== "string")
      validation(
        "string_type",
        ["filters", "artist_id"],
        "Input should be a valid string",
        input.artist_id,
      );
    if ([...input.artist_id].length < 1 || [...input.artist_id].length > 64)
      validation(
        input.artist_id.length < 1 ? "string_too_short" : "string_too_long",
        ["filters", "artist_id"],
        `String should have ${input.artist_id.length < 1 ? "at least 1 character" : "at most 64 characters"}`,
        input.artist_id,
      );
    result.artist_id = input.artist_id;
  }
  for (const key of [
    "year_min",
    "year_max",
    "popularity_min",
    "popularity_max",
  ])
    result[key] = integer(
      input[key],
      key,
      key.startsWith("year") ? 1800 : 0,
      key.startsWith("year") ? 2200 : 100,
      ["filters"],
    );
  for (const key of ["year", "popularity"])
    if (result[`${key}_min`] > result[`${key}_max`])
      validation(
        "value_error",
        ["filters"],
        `Value error, ${key}_min must be less than or equal to ${key}_max`,
        input,
        { error: {} },
      );
  return result;
}
async function bodyOf(request) {
  try {
    const body = await request.json();
    object(body);
    return body;
  } catch (error) {
    if (error instanceof RoomError) throw error;
    validation("json_invalid", [], "JSON decode error", {});
  }
}
async function room(env, code) {
  const value = await first(
    env,
    "SELECT * FROM rooms WHERE code = ? AND delete_at > ?",
    [code, now()],
  );
  if (!value) fail(404, "Challenge unavailable");
  return value;
}
function score(rows) {
  const counts = Array.from(
    { length: 6 },
    (_, index) =>
      rows.filter((row) => row.result === `clue_${index + 1}`).length,
  );
  return [
    counts.reduce((sum, count, index) => sum + (6 - index) * count, 0),
    counts.reduce((a, b) => a + b, 0),
    ...counts,
  ];
}
function compareScores(a, b) {
  for (let i = 0; i < a.length; i++) if (a[i] !== b[i]) return b[i] - a[i];
  return 0;
}
const publicGuesses = (guesses) =>
  guesses.map((guess) =>
    Object.fromEntries(
      Object.entries(guess).filter(([key]) => !key.startsWith("_")),
    ),
  );
async function summary(env, value, id) {
  const closed = now() >= value.closes_at;
  const [participants, rounds] = await Promise.all([
    all(
      env,
      "SELECT * FROM participants WHERE room_code = ? ORDER BY joined_at, guest_id",
      [value.code],
    ),
    all(env, "SELECT * FROM participant_rounds WHERE room_code = ?", [
      value.code,
    ]),
  ]);
  const ranked = participants
    .map((participant) => {
      const own = rounds
        .filter((row) => row.guest_id === participant.guest_id)
        .sort((a, b) => a.position - b.position);
      return { participant, own, score: score(own) };
    })
    .sort((a, b) => compareScores(a.score, b.score));
  let previous,
    rank = 0;
  const leaderboard = await Promise.all(
    ranked.map(async (entry, index) => {
      if (!previous || compareScores(previous, entry.score)) rank = index + 1;
      previous = entry.score;
      const assignedRank = rank;
      return {
        player_id: hex(
          await crypto.subtle.digest(
            "SHA-256",
            encoder.encode(`${value.code}:${entry.participant.guest_id}`),
          ),
        ).slice(0, 16),
        display_name: entry.participant.display_name,
        rank: assignedRank,
        points: entry.score[0],
        correct_count: entry.score[1],
        completed_count: entry.own.filter((row) => row.result != null).length,
        is_you: entry.participant.guest_id === id,
        songs: entry.own.map((row) => ({
          position: row.position,
          points: row.result != null || closed ? score([row])[0] : null,
          result: row.result,
        })),
      };
    }),
  );
  return {
    code: value.code,
    filters: JSON.parse(value.filters_json),
    song_count: value.song_count,
    created_at: timestamp(value.created_at),
    closes_at: timestamp(value.closes_at),
    delete_at: timestamp(value.delete_at),
    status: closed ? "closed" : "active",
    participant_count: participants.length,
    is_member: leaderboard.some((entry) => entry.is_you),
    leaderboard,
  };
}
async function state(env, value, id) {
  const member = await first(
    env,
    "SELECT * FROM participants WHERE room_code = ? AND guest_id = ?",
    [value.code, id],
  );
  if (!member) fail(403, "Join this challenge first");
  const rows = await all(
    env,
    "SELECT pr.*, rs.preview_url, rs.reveal_json FROM participant_rounds pr JOIN room_songs rs USING(room_code, position) WHERE pr.room_code = ? AND pr.guest_id = ? ORDER BY position",
    [value.code, id],
  );
  const closed = now() >= value.closes_at;
  const current = rows.find((row) => row.result == null);
  return {
    code: value.code,
    guest_id: id,
    display_name: member.display_name,
    closed,
    closes_at: timestamp(value.closes_at),
    // The existing client queues a frozen playlist for optimistic local play.
    playlist: rows.map((row) => ({
      position: row.position,
      reveal: JSON.parse(row.reveal_json),
    })),
    completed_count: rows.filter((row) => row.result != null).length,
    total_points: score(rows)[0],
    current_round:
      closed || !current
        ? null
        : {
            position: current.position,
            preview_url: current.preview_url,
            clue_number: current.clue_number,
            guesses: publicGuesses(JSON.parse(current.guesses_json)),
            version: current.version,
          },
    results: rows
      .filter((row) => row.result != null || closed)
      .map((row) => ({
        position: row.position,
        result: row.result || "not_guessed",
        completed_at: timestamp(
          row.result != null ? row.completed_at : value.closes_at,
        ),
        reveal: JSON.parse(row.reveal_json),
      })),
  };
}
function songFields(song, reveal = false) {
  const keys = [
    "id",
    "title",
    "artist",
    "album",
    "release_year",
    "artwork_url",
    "popularity_score",
  ];
  if (reveal) keys.push("preview_url", "apple_music_url", "spotify_url");
  const value = Object.fromEntries(keys.map((key) => [key, song[key] ?? null]));
  if (reveal) value.genres = JSON.parse(song.genres_json || "[]");
  return value;
}
async function createRoom(request, env, body) {
  extras(body, ["display_name", "filters", "song_count", "duration_hours"]);
  const display = name(body);
  if (body.filters === undefined)
    validation("missing", ["filters"], "Field required", body);
  const selectedFilters = filters(body.filters);
  const count = integer(body.song_count, "song_count", 1, 20, [], 10),
    duration = integer(body.duration_hours, "duration_hours", 1, 24, [], 24);
  origin(request);
  const id = await guest(request, env);
  const clauses = [
    "s.enabled = 1",
    "s.release_year BETWEEN ? AND ?",
    "s.popularity_score BETWEEN ? AND ?",
    "s.apple_track_id IS NOT NULL AND s.apple_track_id <> ''",
    "s.preview_url <> ''",
  ];
  const params = [
    selectedFilters.year_min,
    selectedFilters.year_max,
    selectedFilters.popularity_min,
    selectedFilters.popularity_max,
  ];
  for (const key of ["genres", "countries"]) {
    const values = [
      ...new Set(
        selectedFilters[key]
          .map((value) =>
            key === "genres"
              ? value.trim().toLowerCase()
              : value.trim().toUpperCase(),
          )
          .filter(Boolean),
      ),
    ].sort();
    if (values.length) {
      clauses.push(
        key === "genres"
          ? `EXISTS(SELECT 1 FROM song_genres sg JOIN genres g ON g.id=sg.genre_id WHERE sg.song_id=s.id AND g.name IN (${values.map(() => "?").join(",")}))`
          : `EXISTS(SELECT 1 FROM song_countries sc JOIN countries c ON c.id=sc.country_id WHERE sc.song_id=s.id AND c.code IN (${values.map(() => "?").join(",")}))`,
      );
      params.push(...values);
    }
  }
  if (selectedFilters.artist_id) {
    clauses.push(
      "EXISTS(SELECT 1 FROM song_artists sa JOIN artists a ON a.id=sa.artist_id WHERE sa.song_id=s.id AND a.musicbrainz_id = ?)",
    );
    params.push(selectedFilters.artist_id);
  }
  const songs = await all(
    env,
    "SELECT s.*, (SELECT json_group_array(name) FROM (SELECT g.name FROM genres g JOIN song_genres sg ON sg.genre_id=g.id WHERE sg.song_id=s.id ORDER BY g.name)) AS genres_json FROM songs s WHERE " +
      clauses.join(" AND ") +
      " ORDER BY random() LIMIT ?",
    [...params, count],
  );
  if (songs.length !== count) fail(422, "Not enough songs match these filters");
  const alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789";
  const code = Array.from(
    crypto.getRandomValues(new Uint8Array(8)),
    (value) => alphabet[value % alphabet.length],
  ).join("");
  const created = now(),
    closes = created + duration * 3600;
  const statements = [
    [
      "INSERT INTO rooms VALUES (?, ?, ?, ?, ?, ?, ?)",
      [
        code,
        id,
        JSON.stringify(selectedFilters),
        count,
        created,
        closes,
        closes + RETENTION,
      ],
    ],
    [
      "INSERT INTO participants VALUES (?, ?, ?, ?)",
      [code, id, display, created],
    ],
  ];
  songs.forEach((song, index) =>
    statements.push(
      [
        "INSERT INTO room_songs VALUES (?, ?, ?, ?, ?)",
        [
          code,
          index + 1,
          song.apple_track_id,
          song.preview_url,
          JSON.stringify(songFields(song, true)),
        ],
      ],
      [
        "INSERT INTO participant_rounds (room_code, guest_id, position) VALUES (?, ?, ?)",
        [code, id, index + 1],
      ],
    ),
  );
  await batch(env, statements);
  return summary(env, await room(env, code), id);
}
async function joinRoom(request, env, code, body) {
  extras(body, ["display_name"]);
  const display = name(body);
  origin(request);
  const id = await guest(request, env);
  const value = await room(env, code);
  const member = await first(
    env,
    "SELECT guest_id FROM participants WHERE room_code = ? AND guest_id = ?",
    [code, id],
  );
  if (member) return state(env, value, id);
  const joined = now();
  await batch(env, [
    [
      "INSERT INTO participants SELECT code, ?, ?, ? FROM rooms WHERE code = ? AND closes_at > ? AND (SELECT COUNT(*) FROM participants WHERE room_code = ?) < 50 ON CONFLICT(room_code, guest_id) DO NOTHING",
      [id, display, joined, code, joined, code],
    ],
    [
      "INSERT INTO participant_rounds (room_code, guest_id, position) SELECT rs.room_code, ?, rs.position FROM room_songs rs JOIN participants p ON p.room_code = rs.room_code AND p.guest_id = ? WHERE rs.room_code = ? ON CONFLICT(room_code, guest_id, position) DO NOTHING",
      [id, id, code],
    ],
  ]);
  if (
    !(await first(
      env,
      "SELECT guest_id FROM participants WHERE room_code = ? AND guest_id = ?",
      [code, id],
    ))
  )
    fail(409, "Challenge is closed or full");
  return state(env, value, id);
}
async function actionRoom(request, env, code, body) {
  extras(body, ["position", "expected_version", "action", "song_id"]);
  const position = integer(body.position, "position", 1, 20),
    version = integer(body.expected_version, "expected_version", 0);
  if (!["guess", "skip", "give_up"].includes(body.action))
    validation(
      "literal_error",
      ["action"],
      "Input should be 'guess', 'skip' or 'give_up'",
      body.action,
      { expected: "'guess', 'skip' or 'give_up'" },
    );
  const songId =
    body.song_id == null ? null : integer(body.song_id, "song_id", 1);
  origin(request);
  const id = await guest(request, env);
  const value = await room(env, code);
  const current = (await state(env, value, id)).current_round;
  const conflict = "Round changed or challenge closed; reload the challenge";
  if (!current || current.position !== position || current.version !== version)
    fail(409, conflict);
  const row = await first(
    env,
    "SELECT pr.*, rs.answer_apple_track_id, rs.reveal_json FROM participant_rounds pr JOIN room_songs rs USING(room_code, position) WHERE pr.room_code = ? AND pr.guest_id = ? AND pr.position = ?",
    [code, id, position],
  );
  if (!row) fail(409, "Round changed; reload the challenge");
  let clue = row.clue_number,
    result = null;
  const guesses = JSON.parse(row.guesses_json);
  if (body.action === "guess") {
    if (songId == null) fail(422, "Choose a song");
    const identity = await first(
      env,
      "SELECT * FROM songs WHERE id = ? AND enabled = 1",
      [songId],
    );
    if (!identity || !identity.apple_track_id) fail(422, "Song unavailable");
    if (
      guesses.some((guess) => guess._apple_track_id === identity.apple_track_id)
    )
      fail(422, "You already guessed this song");
    guesses.push({
      ...songFields(identity),
      _apple_track_id: identity.apple_track_id,
    });
    if (identity.apple_track_id === row.answer_apple_track_id)
      result = `clue_${clue}`;
  }
  if (!result) {
    if (body.action === "give_up" || clue === 6) result = "not_guessed";
    else clue++;
  }
  const actionTime = now();
  const changed = await stmt(
    env,
    "UPDATE participant_rounds SET clue_number = ?, guesses_json = ?, result = ?, version = version + 1, completed_at = ? WHERE room_code = ? AND guest_id = ? AND position = ? AND version = ? AND result IS NULL AND EXISTS(SELECT 1 FROM rooms WHERE code = ? AND closes_at > ? AND delete_at > ? AND closes_at > unixepoch()) AND NOT EXISTS(SELECT 1 FROM participant_rounds earlier WHERE earlier.room_code = ? AND earlier.guest_id = ? AND earlier.position < ? AND earlier.result IS NULL)",
    [
      clue,
      JSON.stringify(guesses),
      result,
      result ? actionTime : null,
      code,
      id,
      position,
      version,
      code,
      actionTime,
      actionTime,
      code,
      id,
      position,
    ],
  ).run();
  if (changed.meta.changes !== 1) fail(409, conflict);
  return {
    state: await state(env, value, id),
    round: {
      position,
      clue_number: clue,
      guesses: publicGuesses(guesses),
      version: version + 1,
      result,
      reveal: result ? JSON.parse(row.reveal_json) : null,
    },
  };
}
export async function handleRoomRequest(
  request,
  env,
  pathname = new URL(request.url).pathname,
) {
  const match = /^\/api\/rooms\/([^/]+)(?:\/(join|me|actions))?$/.exec(
    pathname,
  );
  if (pathname !== "/api/guest-session" && pathname !== "/api/rooms" && !match)
    return null;
  try {
    if (pathname === "/api/guest-session") {
      if (request.method !== "POST")
        return json({ detail: "Method Not Allowed" }, 405, { Allow: "POST" });
      origin(request);
      const id =
        (await guest(request, env, false)) ||
        hex(crypto.getRandomValues(new Uint8Array(16)));
      const payload = `${id}.${now() + SESSION_AGE}`;
      const signature = hex(
        await crypto.subtle.sign(
          "HMAC",
          await signingKey(env),
          encoder.encode(payload),
        ),
      );
      const url = new URL(request.url),
        secure =
          url.protocol === "https:" ||
          !["localhost", "127.0.0.1", "[::1]"].includes(url.hostname);
      return json({ guest_id: id }, 200, {
        "Set-Cookie": `${COOKIE}=${payload}.${signature}; HttpOnly; Max-Age=${SESSION_AGE}; Path=/; SameSite=lax${secure ? "; Secure" : ""}`,
      });
    }
    const operation = match?.[2],
      expectedMethod =
        pathname === "/api/rooms" ||
        operation === "join" ||
        operation === "actions"
          ? "POST"
          : "GET";
    if (request.method !== expectedMethod)
      return json({ detail: "Method Not Allowed" }, 405, {
        Allow: expectedMethod,
      });
    if (pathname === "/api/rooms")
      return json(await createRoom(request, env, await bodyOf(request)));
    const code = decodeURIComponent(match[1]);
    if (operation === "join")
      return json(await joinRoom(request, env, code, await bodyOf(request)));
    if (operation === "actions")
      return json(await actionRoom(request, env, code, await bodyOf(request)));
    if (operation === "me") {
      const value = await room(env, code);
      return json(await state(env, value, await guest(request, env)));
    }
    const value = await room(env, code);
    return json(await summary(env, value, await guest(request, env, false)));
  } catch (error) {
    if (error instanceof RoomError)
      return json({ detail: error.detail }, error.status);
    throw error;
  }
}
export async function cleanupExpiredRooms(env, cutoff = now()) {
  let total = 0;
  while (true) {
    const deleted = (
      await stmt(
        env,
        "DELETE FROM rooms WHERE code IN (SELECT code FROM rooms WHERE delete_at <= ? LIMIT 100)",
        [cutoff],
      ).run()
    ).meta.changes;
    total += deleted;
    if (deleted < 100) return total;
  }
}
