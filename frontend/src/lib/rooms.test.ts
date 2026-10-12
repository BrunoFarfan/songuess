import { afterEach, describe, expect, it, vi } from "vitest";
import { addOrUpdateRound } from "./roundHistory";
import {
  RoomPlayback,
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
  closes_at: "2099-01-01T00:00:00Z",
  playlist: [],
  completed_count: 0,
  total_points: 0,
  current_round: { ...round, clue_number: 2, version: 1 },
  results: [],
};
afterEach(() => vi.unstubAllGlobals());
describe("challenge action recovery", () => {
  it("sends version and selected song to server", async () => {
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

const reveal = {
  id: 42,
  title: "Song",
  artist: "Artist",
  album: null,
  release_year: 2000,
  artwork_url: null,
  popularity_score: null,
  genres: [],
  preview_url: "/audio.mp3",
  apple_music_url: null,
  spotify_url: null,
};
const playable = (): RoomState => ({
  ...state,
  current_round: { ...round, guesses: [] },
  playlist: [
    { position: 1, reveal },
    { position: 2, reveal: { ...reveal, id: 43 } },
  ],
});
function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: Error) => void;
  const promise = new Promise<T>((yes, no) => {
    resolve = yes;
    reject = no;
  });
  return { promise, resolve, reject };
}
const acknowledge = (state: RoomState) => new Response(JSON.stringify({ state, round: {} }));
async function flush() {
  for (let index = 0; index < 30; index++) await Promise.resolve();
}

describe("prefetched challenge playback", () => {
  it("scores a correct final clue and fails a wrong final guess immediately", () => {
    vi.stubGlobal("fetch", vi.fn().mockReturnValue(new Promise(() => {})));
    const finalClue = { ...playable(), current_round: { ...round, clue_number: 6, version: 5 } };
    const correct = new RoomPlayback(finalClue, vi.fn());
    correct.action("guess", 42);
    expect(correct.snapshot.result?.result).toBe("clue_6");
    expect(correct.snapshot.state.total_points).toBe(1);
    const wrong = new RoomPlayback(finalClue, vi.fn());
    wrong.action("guess", 99);
    expect(wrong.snapshot.result?.result).toBe("not_guessed");
    expect(wrong.snapshot.state.total_points).toBe(0);
  });
  it("predicts rapid clues immediately and serializes exact versions", async () => {
    const first = deferred<Response>();
    const second = deferred<Response>();
    const fetchMock = vi
      .fn()
      .mockReturnValueOnce(first.promise)
      .mockReturnValueOnce(second.promise);
    vi.stubGlobal("fetch", fetchMock);
    const initial = playable();
    const playback = new RoomPlayback(initial, vi.fn());
    playback.action("skip");
    const firstPrediction = playback.snapshot.state;
    playback.action("skip");
    expect(playback.snapshot.state.current_round).toMatchObject({ clue_number: 3, version: 2 });
    expect(playback.snapshot.confirmedState).toBe(initial);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    first.resolve(acknowledge(firstPrediction));
    await flush();
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(JSON.parse(fetchMock.mock.calls[1][1].body)).toMatchObject({
      position: 1,
      expected_version: 1,
    });
    expect(playback.snapshot.state.current_round?.clue_number).toBe(3);
    second.resolve(acknowledge(playback.snapshot.state));
    await playback.settled();
    expect(playback.snapshot.confirmedState.current_round?.version).toBe(2);
  });
  it("reveals and advances songs without waiting, and older acknowledgements do not restore a reveal", async () => {
    const first = deferred<Response>();
    const second = deferred<Response>();
    const fetchMock = vi
      .fn()
      .mockReturnValueOnce(first.promise)
      .mockReturnValueOnce(second.promise);
    vi.stubGlobal("fetch", fetchMock);
    const playback = new RoomPlayback(playable(), vi.fn());
    playback.action("guess", 42);
    const completed = playback.snapshot.state;
    expect(playback.snapshot.result?.result).toBe("clue_1");
    expect(playback.snapshot.confirmedState.results).toHaveLength(0);
    playback.next();
    playback.action("skip");
    expect(playback.snapshot.state.current_round).toMatchObject({ position: 2, clue_number: 2 });
    first.resolve(acknowledge(completed));
    await flush();
    expect(playback.snapshot.result).toBeNull();
    expect(JSON.parse(fetchMock.mock.calls[1][1].body)).toMatchObject({
      position: 2,
      expected_version: 0,
    });
    second.resolve(acknowledge(playback.snapshot.state));
    await playback.settled();
    expect(playback.snapshot.result).toBeNull();
    expect(playback.snapshot.confirmedState.results).toHaveLength(1);
  });
  it("drops dependent queued clicks on lost-response recovery without replay", async () => {
    const request = deferred<Response>();
    const recovered = { ...playable(), current_round: { ...round, clue_number: 2, version: 1 } };
    const fetchMock = vi
      .fn()
      .mockReturnValueOnce(request.promise)
      .mockResolvedValueOnce(new Response(JSON.stringify(recovered)));
    vi.stubGlobal("fetch", fetchMock);
    const playback = new RoomPlayback(playable(), vi.fn());
    playback.action("skip");
    playback.action("skip");
    playback.action("give_up");
    request.reject(new TypeError("lost response"));
    await playback.settled();
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(playback.snapshot.state).toEqual(recovered);
    expect(playback.snapshot.result).toBeNull();
    expect(playback.snapshot.error).toContain("Progress refreshed");
  });
  it("blocks if recovery fails", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("offline")));
    const playback = new RoomPlayback(playable(), vi.fn());
    playback.action("skip");
    await playback.settled();
    expect(playback.snapshot.uncertain).toBe(true);
    playback.action("skip");
    expect(fetch).toHaveBeenCalledTimes(2);
  });
  it("finishes accepted writes after navigation without emitting stale UI updates", async () => {
    const request = deferred<Response>();
    const fetchMock = vi.fn().mockReturnValueOnce(request.promise);
    vi.stubGlobal("fetch", fetchMock);
    const changed = vi.fn();
    const playback = new RoomPlayback(playable(), changed);
    playback.action("skip");
    const firstPrediction = playback.snapshot.state;
    playback.action("skip");
    const secondPrediction = playback.snapshot.state;
    fetchMock.mockResolvedValueOnce(acknowledge(secondPrediction));
    playback.dispose();
    changed.mockClear();
    request.resolve(acknowledge(firstPrediction));
    await playback.settled();
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(changed).not.toHaveBeenCalled();
    expect(playback.snapshot.confirmedState.current_round?.clue_number).toBe(3);
  });
  it("drops predictions if authoritative success disagrees with the predicted guess", async () => {
    const request = deferred<Response>();
    const fetchMock = vi.fn().mockReturnValueOnce(request.promise);
    vi.stubGlobal("fetch", fetchMock);
    const playback = new RoomPlayback(playable(), vi.fn());
    playback.action("guess", 42);
    playback.next();
    playback.action("skip");
    const authoritative = {
      ...playable(),
      current_round: { ...round, clue_number: 2, version: 1 },
    };
    request.resolve(acknowledge(authoritative));
    await playback.settled();
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(playback.snapshot.state).toEqual(authoritative);
  });
  it("prevents duplicate guesses, reveals failures at last clue, and respects deadline", () => {
    vi.stubGlobal("fetch", vi.fn().mockReturnValue(new Promise(() => {})));
    const playback = new RoomPlayback(playable(), vi.fn());
    const wrong = { ...reveal, id: 99, title: "Wrong song" };
    playback.action("guess", 99, wrong);
    playback.action("guess", 99, wrong);
    expect(playback.snapshot.state.current_round?.guesses).toEqual([wrong]);
    expect(playback.snapshot.state.current_round?.clue_number).toBe(2);
    for (let clue = 2; clue <= 6; clue++) playback.action("skip");
    expect(playback.snapshot.result?.result).toBe("not_guessed");
    const expired = new RoomPlayback({ ...playable(), closes_at: "2000-01-01T00:00:00Z" }, vi.fn());
    expired.action("give_up");
    expect(expired.snapshot.state.completed_count).toBe(0);
    expect(expired.snapshot.error).toContain("closed");
  });
  it("gives up immediately and bounds clicks after the final reveal", () => {
    vi.stubGlobal("fetch", vi.fn().mockReturnValue(new Promise(() => {})));
    const playback = new RoomPlayback(
      { ...playable(), playlist: [{ position: 1, reveal }] },
      vi.fn(),
    );
    playback.action("give_up");
    expect(playback.snapshot.result?.result).toBe("not_guessed");
    playback.next();
    playback.action("skip");
    expect(playback.snapshot.state.current_round).toBeNull();
    expect(fetch).toHaveBeenCalledTimes(1);
  });
});
