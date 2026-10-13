import {
  getFilters,
  getContextFilters,
  searchSongs,
  searchArtists,
  chooseRound,
  revealSong,
} from "./catalog-worker.mjs";
import { handleRoomRequest, cleanupExpiredRooms } from "./rooms-worker.mjs";

const json = (body, status = 200) => Response.json(body, { status });

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const path = url.pathname.replace(/\/$/, "");
    if (!path.startsWith("/api/")) return env.ASSETS.fetch(request);
    try {
      const methods = {
        "/api/health": "GET",
        "/api/filters": "GET",
        "/api/filters/context": "POST",
        "/api/songs/search": "GET",
        "/api/artists/search": "GET",
        "/api/round": "POST",
      };
      const expected =
        methods[path] ?? (/^\/api\/songs\/-?\d+$/.test(path) ? "GET" : null);
      if (expected && request.method !== expected) {
        return Response.json(
          { detail: "Method Not Allowed" },
          { status: 405, headers: { Allow: expected } },
        );
      }
      if (request.method === "GET" && path === "/api/health")
        return json({ status: "ok" });
      if (request.method === "GET" && path === "/api/filters")
        return json(await getFilters(env));
      if (request.method === "POST" && path === "/api/filters/context")
        return json(await getContextFilters(env, await request.json()));
      if (request.method === "GET" && path === "/api/songs/search")
        return json(await searchSongs(env, url.searchParams));
      if (request.method === "GET" && path === "/api/artists/search")
        return json(await searchArtists(env, url.searchParams));
      if (request.method === "POST" && path === "/api/round") {
        const round = await chooseRound(env, await request.json());
        return round
          ? json(round)
          : json(
              {
                detail: {
                  code: "NO_MATCHING_SONGS",
                  message: "No songs match these filters yet.",
                },
              },
              404,
            );
      }
      const song = path.match(/^\/api\/songs\/(-?\d+)$/);
      if (song && request.method === "GET") {
        const result = await revealSong(env, Number(song[1]));
        return result
          ? json(result)
          : json(
              {
                detail: {
                  code: "SONG_NOT_FOUND",
                  message: "This song is unavailable.",
                },
              },
              404,
            );
      }
      const room = await handleRoomRequest(request, env, path);
      return room ?? json({ detail: "Not Found" }, 404);
    } catch (error) {
      if (error instanceof SyntaxError || error instanceof URIError)
        return json({ detail: "Invalid JSON body." }, 422);
      if (
        Number.isInteger(error.status) &&
        error.status >= 400 &&
        error.status < 500
      ) {
        return json({ detail: error.detail ?? error.message }, error.status);
      }
      console.error("API request failed", { path, message: String(error) });
      return json(
        {
          detail: {
            code: "SERVICE_UNAVAILABLE",
            message:
              "The catalog is temporarily unavailable. Please try again.",
          },
        },
        503,
      );
    }
  },
  async scheduled(_controller, env) {
    const deleted = await cleanupExpiredRooms(env);
    console.log(`Challenge room cleanup: deleted ${deleted} expired rooms`);
  },
};
