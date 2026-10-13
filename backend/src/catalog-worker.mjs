// Native D1 catalog API. Only completed values are cached: D1 promises belong to
// their originating request and must never be awaited by a different request.
const caches = new WeakMap();
const TTL = 300_000;
const MAX_ENTRIES = 128;
const MAX_WEIGHT = 50_000;
const columns =
  "s.id, s.title, s.artist, s.album, s.release_year, s.artwork_url, s.popularity_score";
function cache(env) {
  let value = caches.get(env.DB);
  if (!value) {
    value = { entries: new Map(), weight: 0 };
    caches.set(env.DB, value);
  }
  return value;
}
function read(env, key) {
  const c = cache(env),
    entry = c.entries.get(key);
  if (!entry) return undefined;
  if (entry.expires <= Date.now()) {
    c.entries.delete(key);
    c.weight -= entry.weight;
    return undefined;
  }
  c.entries.delete(key);
  c.entries.set(key, entry);
  return entry.value;
}
function write(env, key, value, weight = 1) {
  const c = cache(env),
    old = c.entries.get(key);
  if (old) {
    c.weight -= old.weight;
    c.entries.delete(key);
  }
  c.entries.set(key, { value, weight, expires: Date.now() + TTL });
  c.weight += weight;
  while (c.entries.size > MAX_ENTRIES || c.weight > MAX_WEIGHT) {
    const first = c.entries.keys().next().value;
    c.weight -= c.entries.get(first).weight;
    c.entries.delete(first);
  }
  return value;
}
const all = async (env, sql, args = []) =>
  (
    await env.DB.prepare(sql)
      .bind(...args)
      .all()
  ).results;
const first = (env, sql, args = []) =>
  env.DB.prepare(sql)
    .bind(...args)
    .first();
function invalid(message) {
  throw Object.assign(new Error(message), { status: 422, detail: message });
}
function integer(value, name, min, max) {
  if (
    typeof value === "boolean" ||
    value === null ||
    value === "" ||
    !Number.isSafeInteger(Number(value)) ||
    Number(value) < min ||
    Number(value) > max
  )
    invalid(`Invalid ${name}`);
  return Number(value);
}
export function validateFilters(body, round = false) {
  if (!body || typeof body !== "object" || Array.isArray(body))
    invalid("Expected an object");
  const allowed = [
    "genres",
    "countries",
    "artist_id",
    "year_min",
    "year_max",
    "popularity_min",
    "popularity_max",
    ...(round ? ["exclude_ids"] : []),
  ];
  if (Object.keys(body).some((k) => !allowed.includes(k)))
    invalid("Unexpected filter field");
  const result = {};
  for (const key of [
    "genres",
    "countries",
    ...(round ? ["exclude_ids"] : []),
  ]) {
    const values = body[key] ?? [];
    if (
      !Array.isArray(values) ||
      values.length > (key === "exclude_ids" ? 500 : 30)
    )
      invalid(`Invalid ${key}`);
    if (key === "exclude_ids")
      result[key] = values.map((v) =>
        integer(v, key, -Number.MAX_SAFE_INTEGER, Number.MAX_SAFE_INTEGER),
      );
    else {
      if (
        values.some(
          (v) =>
            typeof v !== "string" ||
            (key === "countries" && !/^[a-z]{2}$/i.test(v)),
        )
      )
        invalid(`Invalid ${key}`);
      result[key] = [
        ...new Set(
          values
            .map((v) =>
              key === "genres" ? v.trim().toLowerCase() : v.toUpperCase(),
            )
            .filter(Boolean),
        ),
      ].sort();
    }
  }
  result.artist_id = body.artist_id ?? null;
  if (
    result.artist_id !== null &&
    (typeof result.artist_id !== "string" ||
      result.artist_id.length < 1 ||
      result.artist_id.length > 64)
  )
    invalid("Invalid artist_id");
  for (const key of [
    "year_min",
    "year_max",
    "popularity_min",
    "popularity_max",
  ]) {
    if (body[key] == null && !round) result[key] = null;
    else
      result[key] = integer(
        body[key],
        key,
        key.startsWith("year") ? 1800 : 0,
        key.startsWith("year") ? 2200 : 100,
      );
  }
  if (
    result.year_min !== null &&
    result.year_max !== null &&
    result.year_min > result.year_max
  )
    invalid("year_min must be less than or equal to year_max");
  if (
    result.popularity_min !== null &&
    result.popularity_max !== null &&
    result.popularity_min > result.popularity_max
  )
    invalid("popularity_min must be less than or equal to popularity_max");
  return result;
}
async function snapshot(env) {
  const cached = read(env, "snapshot");
  if (cached) return cached;
  const row = await first(
    env,
    "SELECT version, payload FROM catalog_runtime WHERE id = 1",
  );
  if (!row)
    throw new Error(
      "Catalog snapshot missing; refresh the catalog_runtime snapshot",
    );
  return write(env, "snapshot", {
    ...JSON.parse(row.payload),
    version: row.version,
  });
}
function relationPool(data, dimension, selections) {
  const selected = new Set(
      data[dimension]
        .filter(([, name]) => selections.includes(name))
        .map(([id]) => id),
    ),
    pool = new Set();
  for (const [song, id] of data[`song_${dimension}`])
    if (selected.has(id)) pool.add(song);
  return pool;
}
function pools(env, data, request) {
  let rows = data.songs;
  const result = [];
  for (let stage = 0; stage < 5; stage++) {
    const key = `pool:${data.version}:${JSON.stringify([request.artist_id, ...(stage >= 1 ? [request.genres] : []), ...(stage >= 2 ? [request.countries] : []), ...(stage >= 3 ? [request.year_min, request.year_max] : []), ...(stage >= 4 ? [request.popularity_min, request.popularity_max] : [])])}`;
    const cached = read(env, key);
    if (cached) rows = cached;
    else {
      if (stage === 0 && request.artist_id) {
        const selected = relationPool(data, "artists", [request.artist_id]);
        rows = rows.filter(([id]) => selected.has(id));
      }
      if (stage === 1 && request.genres.length) {
        const selected = relationPool(data, "genres", request.genres);
        rows = rows.filter(([id]) => selected.has(id));
      }
      if (stage === 2 && request.countries.length) {
        const selected = relationPool(data, "countries", request.countries);
        rows = rows.filter(([id]) => selected.has(id));
      }
      if (
        stage === 3 &&
        (request.year_min !== null || request.year_max !== null)
      )
        rows = rows.filter(
          ([, year]) =>
            (request.year_min === null || year >= request.year_min) &&
            (request.year_max === null || year <= request.year_max),
        );
      if (
        stage === 4 &&
        (request.popularity_min !== null || request.popularity_max !== null)
      )
        rows = rows.filter(
          ([, , score]) =>
            (request.popularity_min === null ||
              score >= request.popularity_min) &&
            (request.popularity_max === null ||
              score <= request.popularity_max),
        );
      write(env, key, rows, Math.ceil(rows.length / 16));
    }
    result.push(rows);
  }
  return result;
}
function bounds(rows, index) {
  let min = Infinity,
    max = -Infinity;
  for (const row of rows) {
    if (row[index] < min) min = row[index];
    if (row[index] > max) max = row[index];
  }
  return rows.length ? [min, max] : [null, null];
}
function facet(data, dimension, rows) {
  const songs = new Set(rows.map(([id]) => id)),
    selected = new Set();
  for (const [song, id] of data[`song_${dimension}`])
    if (songs.has(song)) selected.add(id);
  return data[dimension]
    .filter(([id]) => selected.has(id))
    .map(([, name]) => name)
    .sort();
}
async function metadata(env, request, contextual) {
  const key = `metadata:${contextual}:${JSON.stringify(request)}`,
    cached = read(env, key);
  if (cached) return cached;
  if (!contextual) {
    // Project the tiny metadata inside D1 instead of parsing a 582KB snapshot
    // on the Worker's 10ms CPU budget. Database execution is off-isolate.
    const row = await first(
      env,
      "SELECT json_extract(payload, '$.genres') AS genres, json_extract(payload, '$.countries') AS countries, (SELECT release_year FROM songs WHERE enabled = 1 AND preview_url <> '' AND popularity_score IS NOT NULL ORDER BY release_year LIMIT 1) AS year_min, (SELECT release_year FROM songs WHERE enabled = 1 AND preview_url <> '' AND popularity_score IS NOT NULL ORDER BY release_year DESC LIMIT 1) AS year_max, json_array_length(payload, '$.songs') AS song_count FROM catalog_runtime WHERE id = 1",
    );
    if (!row)
      throw new Error(
        "Catalog snapshot missing; refresh the catalog_runtime snapshot",
      );
    return write(env, key, {
      genres: JSON.parse(row.genres)
        .map(([, name]) => name)
        .sort(),
      countries: JSON.parse(row.countries)
        .map(([, code]) => code)
        .sort(),
      year_min: row.year_min,
      year_max: row.year_max,
      popularity_min: 0,
      popularity_max: 100,
      song_count: row.song_count,
    });
  }
  const data = await snapshot(env),
    stages = pools(env, data, request);
  const [year_min, year_max] = bounds(stages[2], 1),
    [pmin, pmax] = bounds(stages[3], 2);
  return write(env, key, {
    genres: facet(data, "genres", stages[0]),
    countries: facet(data, "countries", stages[1]),
    year_min,
    year_max,
    popularity_min: pmin ?? 0,
    popularity_max: pmax ?? 0,
    song_count: stages[4].length,
  });
}
export const getFilters = async (env) =>
  metadata(env, validateFilters({}), false);
export const getContextFilters = async (env, body) =>
  metadata(env, validateFilters(body), true);
function liveFilter(request) {
  const clauses = [
    "s.enabled = 1",
    "s.preview_url <> ''",
    "s.release_year BETWEEN ? AND ?",
    "s.popularity_score BETWEEN ? AND ?",
  ];
  const args = [
    request.year_min,
    request.year_max,
    request.popularity_min,
    request.popularity_max,
  ];
  for (const [dimension, field, values] of [
    ["genres", "name", request.genres],
    ["countries", "code", request.countries],
    ["artists", "musicbrainz_id", request.artist_id ? [request.artist_id] : []],
  ]) {
    if (values.length) {
      const singular =
        dimension === "countries" ? "country" : dimension.slice(0, -1);
      clauses.push(
        `EXISTS (SELECT 1 FROM song_${dimension} rel JOIN ${dimension} d ON d.id = rel.${singular}_id WHERE rel.song_id = s.id AND d.${field} IN (${values.map(() => "?").join(",")}))`,
      );
      args.push(...values);
    }
  }
  return { clauses, args };
}
export async function chooseRound(env, body) {
  const request = validateFilters(body, true),
    filter = liveFilter(request),
    exclude = new Set(request.exclude_ids);
  let data = await snapshot(env),
    available = pools(env, data, request)[4].filter(([id]) => !exclude.has(id));
  for (let attempt = 0; attempt < 3 && available.length; attempt++) {
    const random = new Uint32Array(1);
    crypto.getRandomValues(random);
    const id =
      available[Math.floor((random[0] / 4294967296) * available.length)][0];
    const row = await first(
      env,
      `SELECT s.id, s.preview_url FROM songs s WHERE s.id = ? AND ${filter.clauses.join(" AND ")}`,
      [id, ...filter.args],
    );
    if (row) return { song_id: row.id, preview_url: row.preview_url };
    if (!attempt) {
      caches.delete(env.DB);
      data = await snapshot(env);
      available = pools(env, data, request)[4].filter(
        ([id]) => !exclude.has(id),
      );
    }
    available = available.filter(([candidate]) => candidate !== id);
  }
  return null;
}
export function normalizeSearch(value) {
  return (
    String(value ?? "")
      .normalize("NFKD")
      .replace(/[^\x00-\x7f]/g, "")
      .toLowerCase()
      .match(/[a-z0-9]+/g)
      ?.join(" ") ?? ""
  );
}
function trigrams(query) {
  const q = query.replaceAll(" ", "");
  return [
    ...new Set(
      Array.from({ length: q.length - 2 }, (_, i) => q.slice(i, i + 3)),
    ),
  ]
    .sort()
    .map((t) => `"${t}"`)
    .join(" OR ");
}
// Linear bigram Dice similarity keeps fuzzy ranking within the Worker CPU
// budget. Exact/prefix/substring/token tiers retain their original priority;
// only the tie order of weak approximate matches can differ from Python.
function similarity(a, b) {
  if (!a.length || !b.length) return 0;
  if (a === b) return 1;
  if (a.length < 2 || b.length < 2) return 0;
  const counts = new Map();
  for (let i = 0; i < a.length - 1; i++) {
    const code = a.charCodeAt(i) * 128 + a.charCodeAt(i + 1);
    counts.set(code, (counts.get(code) ?? 0) + 1);
  }
  let shared = 0;
  for (let i = 0; i < b.length - 1; i++) {
    const code = b.charCodeAt(i) * 128 + b.charCodeAt(i + 1),
      count = counts.get(code) ?? 0;
    if (count) {
      shared++;
      counts.set(code, count - 1);
    }
  }
  return (2 * shared) / (a.length + b.length - 2);
}
function compare(a, b) {
  for (let i = 0; i < a.length; i++) {
    if (a[i] < b[i]) return -1;
    if (a[i] > b[i]) return 1;
  }
  return 0;
}
function tier(fields, compact, query) {
  if (fields.includes(compact)) return 0;
  if (fields.some((f) => f.startsWith(compact))) return 1;
  if (fields.some((f) => f.includes(compact))) return 2;
  return query.split(" ").every((t) => fields.join(" ").includes(t)) ? 3 : 4;
}
function rankSong(row, query) {
  const fields = [
    normalizeSearch(row.title),
    normalizeSearch(row.artist),
    normalizeSearch(row.album),
    String(row.release_year),
  ];
  const compact = fields.map((f) => f.replaceAll(" ", "")),
    q = query.replaceAll(" ", "");
  const priority = compact.indexOf(q);
  let category = tier(compact, q, query);
  if (
    category === 4 &&
    query.split(" ").every((t) => fields.join(" ").includes(t))
  )
    category = 3;
  return [
    category,
    priority < 0 ? 4 : priority,
    -(category === 0
      ? 1
      : Math.max(
          ...compact.map((f) => similarity(q, f)),
          similarity(q, compact.join("")),
        )),
    -(row.popularity_score ?? 0),
    fields[0],
    fields[1],
    row.id,
  ];
}
// Six keyed Feistel rounds and cycle walking produce stable, disjoint browse
// pages without shuffling or hydrating the entire catalog for every seed.
export function browseIndex(index, total, seed) {
  if (total <= 1) return index;
  const bits = Math.ceil(Math.ceil(Math.log2(total)) / 2),
    mask = (1 << bits) - 1;
  let position = index;
  do {
    let left = position >>> bits,
      right = position & mask;
    for (let round = 0; round < 6; round++) {
      let hash = (seed ^ Math.imul(round + 1, 0x9e3779b9) ^ right) >>> 0;
      hash = Math.imul(hash ^ (hash >>> 16), 0x85ebca6b);
      hash = Math.imul(hash ^ (hash >>> 13), 0xc2b2ae35);
      hash ^= hash >>> 16;
      [left, right] = [right, left ^ (hash & mask)];
    }
    position = (left << bits) | right;
  } while (position >= total);
  return position;
}
export async function searchSongs(env, params) {
  const raw = params.get("q") ?? "";
  if (raw.length > 120) invalid("Invalid q");
  const limit = integer(params.get("limit") ?? 40, "limit", 1, 100),
    offset = integer(
      params.get("offset") ?? 0,
      "offset",
      0,
      Number.MAX_SAFE_INTEGER,
    ),
    seed = integer(params.get("seed") ?? 0, "seed", 0, 4294967295);
  const query = normalizeSearch(raw);
  let items, total;
  if (!query) {
    let ids = read(env, "browse");
    if (!ids) {
      const cachedSnapshot = read(env, "snapshot");
      const projected =
        cachedSnapshot ??
        (await first(
          env,
          "SELECT json_extract(payload, '$.songs') AS songs, json_extract(payload, '$.browse_count') AS browse_count FROM catalog_runtime WHERE id = 1",
        ));
      const songs =
        typeof projected.songs === "string"
          ? JSON.parse(projected.songs)
          : projected.songs;
      ids =
        songs.length === projected.browse_count
          ? songs.map(([id]) => id)
          : (
              await all(
                env,
                "SELECT id FROM songs WHERE enabled = 1 AND preview_url <> '' ORDER BY id",
              )
            ).map((r) => r.id);
      write(env, "browse", ids, Math.ceil(ids.length / 16));
    }
    total = ids.length;
    const page = [];
    for (let i = offset; i < Math.min(offset + limit, total); i++)
      page.push(ids[browseIndex(i, total, seed)]);
    const rows = page.length
      ? await all(
          env,
          `SELECT ${columns} FROM json_each(?) page CROSS JOIN songs s ON s.id = CAST(page.value AS INTEGER) WHERE s.enabled = 1 AND s.preview_url <> ''`,
          [JSON.stringify(page)],
        )
      : [];
    const byId = new Map(rows.map((r) => [r.id, r]));
    items = page.map((id) => byId.get(id)).filter(Boolean);
  } else {
    let rows = read(env, `search:${query}`);
    if (!rows) {
      const compact = query.replaceAll(" ", "");
      const candidates =
        compact.length < 3
          ? await all(
              env,
              `SELECT ${columns} FROM songs s JOIN (SELECT song_id FROM song_search WHERE normalized_title_compact LIKE ? UNION SELECT song_id FROM song_search WHERE normalized_artist_compact LIKE ? UNION SELECT song_id FROM song_search WHERE normalized_album_compact LIKE ?) candidates ON candidates.song_id = s.id WHERE s.enabled = 1 AND s.preview_url <> '' LIMIT ?`,
              [`${compact}%`, `${compact}%`, `${compact}%`, 500],
            )
          : await all(
              env,
              `SELECT ${columns} FROM song_search_fts f JOIN songs s ON s.id = CAST(f.song_id AS INTEGER) WHERE song_search_fts MATCH ? AND s.enabled = 1 AND s.preview_url <> '' ORDER BY f.rank LIMIT ?`,
              [trigrams(query), 500],
            );
      rows = candidates
        .map((row) => [rankSong(row, query), row])
        .sort((a, b) => compare(a[0], b[0]))
        .map(([, row]) => row);
      write(env, `search:${query}`, rows, rows.length);
    }
    total = rows.length;
    items = rows.slice(offset, offset + limit);
  }
  return {
    items,
    offset,
    limit,
    total,
    has_more: offset + items.length < total,
  };
}
function rankAlias(alias, query) {
  const normalized = normalizeSearch(alias),
    compact = normalized.replaceAll(" ", ""),
    q = query.replaceAll(" ", "");
  let category = tier([compact], q, query);
  if (category === 4 && query.split(" ").every((t) => normalized.includes(t)))
    category = 3;
  return [category, -similarity(q, compact), normalized];
}
export async function searchArtists(env, params) {
  const raw = params.get("q");
  if (!raw || raw.length > 120) invalid("Invalid q");
  const query = normalizeSearch(raw);
  if (!query) return [];
  const key = `artists:${query}`,
    cached = read(env, key);
  if (cached) return cached;
  const compact = query.replaceAll(" ", "");
  const candidates =
    compact.length < 3
      ? await all(
          env,
          "SELECT DISTINCT artist_id FROM artist_search_aliases WHERE normalized_alias_compact LIKE ? LIMIT 100",
          [`${compact}%`],
        )
      : await all(
          env,
          "SELECT DISTINCT CAST(artist_id AS INTEGER) AS artist_id FROM artist_search_fts WHERE artist_search_fts MATCH ? LIMIT 100",
          [trigrams(query)],
        );
  if (!candidates.length) return write(env, key, []);
  const rows = await all(
    env,
    "SELECT a.id, a.musicbrainz_id, a.name, a.disambiguation, sa.credited_name, sa.song_id FROM artists a JOIN song_artists sa ON sa.artist_id = a.id JOIN songs s ON s.id = sa.song_id WHERE s.enabled = 1 AND s.preview_url <> '' AND a.id IN (SELECT CAST(value AS INTEGER) FROM json_each(?)) ORDER BY a.id, sa.song_id",
    [
      JSON.stringify(
        [...new Set(candidates.map((r) => r.artist_id))].sort((a, b) => a - b),
      ),
    ],
  );
  const artists = new Map();
  for (const row of rows) {
    let artist = artists.get(row.id);
    if (!artist)
      artists.set(
        row.id,
        (artist = { ...row, names: new Set(), songs: new Set() }),
      );
    artist.names.add(row.credited_name);
    artist.songs.add(row.song_id);
  }
  const ranked = [];
  for (const artist of artists.values()) {
    const aliases = [
      artist.name,
      ...[...artist.names].sort((a, b) =>
        compare([normalizeSearch(a), a], [normalizeSearch(b), b]),
      ),
    ];
    let best = aliases[0],
      rank = rankAlias(best, query);
    for (const alias of aliases.slice(1)) {
      const candidate = rankAlias(alias, query);
      if (compare(candidate, rank) < 0) {
        rank = candidate;
        best = alias;
      }
    }
    if (rank[0] === 4) continue;
    ranked.push([
      [
        ...rank,
        -artist.songs.size,
        normalizeSearch(artist.name),
        artist.musicbrainz_id,
      ],
      {
        id: artist.musicbrainz_id,
        name: best,
        disambiguation: artist.disambiguation,
        song_count: artist.songs.size,
      },
    ]);
  }
  return write(
    env,
    key,
    ranked
      .sort((a, b) => compare(a[0], b[0]))
      .slice(0, 10)
      .map(([, artist]) => artist),
    Math.min(ranked.length, 10),
  );
}
export async function revealSong(env, id) {
  id = integer(
    id,
    "song_id",
    -Number.MAX_SAFE_INTEGER,
    Number.MAX_SAFE_INTEGER,
  );
  const row = await first(
    env,
    "SELECT id, title, artist, album, release_year, artwork_url, popularity_score, preview_url, apple_music_url, spotify_url FROM songs WHERE id = ? AND enabled = 1",
    [id],
  );
  if (!row) return null;
  const genres = await all(
    env,
    "SELECT g.name FROM genres g JOIN song_genres sg ON sg.genre_id = g.id LEFT JOIN song_genre_evidence sge ON sge.song_id = sg.song_id AND sge.genre_id = sg.genre_id WHERE sg.song_id = ? ORDER BY COALESCE(sge.rank, 999), g.name COLLATE NOCASE",
    [id],
  );
  return { ...row, genres: genres.map((g) => g.name) };
}
