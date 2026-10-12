import { afterEach, describe, expect, it, vi } from "vitest";
import { addOrUpdateRound } from "./roundHistory";
import {
  roomFilters,
  roomHistory,
  submitRoomAction,
  type RoomRound,
  type RoomState,
} from "./rooms";
const round: RoomRound = {
  position: 1,
  preview_url: "/audio.mp3",
  clue_number: 1,
  guesses: [],
  version: 0,
};
const state: RoomState = {
  code: "ABCDEFGH",
  guest_id: "guest",
  display_name: "Player",
  closed: false,
  completed_count: 0,
  total_points: 0,
  current_round: { ...round, clue_number: 2, version: 1 },
  results: [],
};
afterEach(() => vi.unstubAllGlobals());
describe("challenge action recovery", () => {
  it("sends version and selected song to server and does not expose an answer", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValue(
        new Response(
          JSON.stringify({ state, round: { ...state.current_round, result: null, reveal: null } }),
        ),
      );
    vi.stubGlobal("fetch", fetchMock);
    const result = await submitRoomAction("ABCDEFGH", round, "guess", 42);
    const [url, options] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/rooms/ABCDEFGH/actions");
    expect(JSON.parse(options.body)).toEqual({
      position: 1,
      expected_version: 0,
      action: "guess",
      song_id: 42,
    });
    expect(result.state.current_round?.clue_number).toBe(2);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
  it.each(["network", "conflict", "server"])(
    "recovers %s failure with a read without consuming another clue",
    async (failure) => {
      const fetchMock = vi.fn();
      if (failure === "network") fetchMock.mockRejectedValueOnce(new TypeError("lost response"));
      else
        fetchMock.mockResolvedValueOnce(
          new Response(JSON.stringify({ detail: { code: "CONFLICT", message: "Changed" } }), {
            status: failure === "conflict" ? 409 : 503,
          }),
        );
      fetchMock.mockResolvedValueOnce(new Response(JSON.stringify(state)));
      vi.stubGlobal("fetch", fetchMock);
      const recovered = await submitRoomAction("ABCDEFGH", round, "skip");
      expect(recovered.recovered).toBe(true);
      expect(recovered.state.current_round?.version).toBe(1);
      expect(fetchMock).toHaveBeenCalledTimes(2);
      expect(fetchMock.mock.calls[1][0]).toBe("/api/rooms/ABCDEFGH/me");
      expect(fetchMock.mock.calls[1][1].method).toBeUndefined();
    },
  );
  it("reports definitive rejection without recovery or retry", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValue(
        new Response(
          JSON.stringify({ detail: { code: "DUPLICATE_GUESS", message: "Already guessed" } }),
          { status: 422 },
        ),
      );
    vi.stubGlobal("fetch", fetchMock);
    await expect(submitRoomAction("ABCDEFGH", round, "guess", 42)).rejects.toThrow(
      "Already guessed",
    );
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
  it("does not replay if recovery also fails", async () => {
    const fetchMock = vi.fn().mockRejectedValue(new TypeError("offline"));
    vi.stubGlobal("fetch", fetchMock);
    await expect(submitRoomAction("ABCDEFGH", round, "give_up")).rejects.toThrow("offline");
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });
  it("keeps cookie identity and sends no client guest identity", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({ state, round })));
    vi.stubGlobal("fetch", fetchMock);
    await submitRoomAction("ABCDEFGH", round, "skip");
    expect(fetchMock.mock.calls[0][1].credentials).toBe("same-origin");
    expect(fetchMock.mock.calls[0][1].body).not.toContain("guest");
  });
});
describe("room records", () => {
  it("uses frozen reveal IDs and updates stable records on repeated resume", () => {
    const completed = {
      ...state,
      completed_count: 1,
      results: [
        {
          position: 1,
          result: "clue_2" as const,
          completed_at: "2026-10-12T12:00:00Z",
          reveal: {
            id: 42,
            title: "Frozen title",
            artist: "Artist",
            album: null,
            release_year: 2000,
            artwork_url: null,
            popularity_score: null,
            genres: [],
            preview_url: "/audio.mp3",
            apple_music_url: null,
            spotify_url: null,
          },
        },
      ],
    };
    const [record] = roomHistory(completed);
    let history = addOrUpdateRound([], record);
    history = addOrUpdateRound(history, roomHistory(completed)[0]);
    expect(history).toHaveLength(1);
    expect(history[0]).toMatchObject({
      id: "room:ABCDEFGH:guest:1",
      songId: 42,
      title: "Frozen title",
    });
    const expired = {
      ...completed,
      closed: true,
      results: [
        ...completed.results,
        { ...completed.results[0], position: 2, result: "not_guessed" as const },
      ],
    };
    expect(roomHistory(expired)).toHaveLength(1);
  });
  it("reuses all setup filters and omits solo exclusions", () => {
    const filters = roomFilters({
      genres: ["rock"],
      countries: ["CL"],
      artist: null,
      yearMin: 1970,
      yearMax: 2000,
      popularityMin: 10,
      popularityMax: 80,
    });
    expect(filters).toEqual({
      genres: ["rock"],
      countries: ["CL"],
      artist_id: null,
      year_min: 1970,
      year_max: 2000,
      popularity_min: 10,
      popularity_max: 80,
    });
    expect(filters).not.toHaveProperty("exclude_ids");
  });
});
