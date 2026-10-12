import type { SetupFilters } from "../components/SetupWizard";
import type { LocalRoundRecord, RoundResult } from "./roundHistory";

export type RoomFilters = {
  genres: string[];
  countries: string[];
  artist_id: string | null;
  year_min: number | null;
  year_max: number | null;
  popularity_min: number | null;
  popularity_max: number | null;
};
export type RoomSong = {
  id: number;
  title: string;
  artist: string;
  album: string | null;
  release_year: number;
  artwork_url: string | null;
  popularity_score: number | null;
};
export type RoomReveal = RoomSong & {
  genres: string[];
  preview_url: string;
  apple_music_url: string | null;
  spotify_url: string | null;
};
export type RoomRound = {
  position: number;
  preview_url: string;
  clue_number: number;
  guesses: RoomSong[];
  version: number;
};
export type RoomResult = {
  position: number;
  result: RoundResult;
  completed_at: string;
  reveal: RoomReveal;
};
export type RoomState = {
  code: string;
  guest_id: string;
  display_name: string;
  closed: boolean;
  completed_count: number;
  total_points: number;
  current_round: RoomRound | null;
  results: RoomResult[];
};
export type RoomSummary = {
  code: string;
  filters: RoomFilters;
  song_count: number;
  created_at: string;
  closes_at: string;
  delete_at: string;
  status: "active" | "closed";
  participant_count: number;
  is_member: boolean;
  leaderboard: {
    display_name: string;
    rank: number;
    points: number;
    correct_count: number;
    completed_count: number;
    is_you: boolean;
  }[];
};
export type RoomAction = "guess" | "skip" | "give_up";
export type ActionResponse = {
  state: RoomState;
  round: Omit<RoomRound, "preview_url"> & { result: RoundResult | null; reveal: RoomReveal | null };
};
export class RoomApiError extends Error {
  constructor(
    public status: number,
    public code: string,
    message: string,
  ) {
    super(message);
  }
}
export async function roomRequest<T>(path: string, body?: unknown): Promise<T> {
  const response = await fetch(
    `/api/${path}`,
    body === undefined
      ? { credentials: "same-origin" }
      : {
          method: "POST",
          credentials: "same-origin",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        },
  );
  if (!response.ok) {
    let message = "Could not reach the challenge. Try refreshing.";
    let code = "";
    try {
      const data = await response.json();
      message = typeof data.detail === "string" ? data.detail : (data.detail?.message ?? message);
      code = data.detail?.code ?? "";
    } catch {}
    throw new RoomApiError(response.status, code, message);
  }
  return response.json();
}
export function roomFilters(filters: SetupFilters): RoomFilters {
  return {
    genres: filters.genres,
    countries: filters.countries,
    artist_id: filters.artist?.id ?? null,
    year_min: filters.yearMin,
    year_max: filters.yearMax,
    popularity_min: filters.popularityMin,
    popularity_max: filters.popularityMax,
  };
}
export function roomHistory(state: RoomState): LocalRoundRecord[] {
  // Deadline standings include unfinished slots; personal history counts completed play only.
  return state.results
    .filter((item) => item.position <= state.completed_count)
    .map((item) => ({
      id: `room:${state.code}:${state.guest_id}:${item.position}`,
      songId: item.reveal.id,
      result: item.result,
      completedAt: item.completed_at,
      title: item.reveal.title,
      artist: item.reveal.artist,
      artworkUrl: item.reveal.artwork_url,
    }));
}
// A lost response may already have committed. Recover authoritative state, never repeat a mutation.
export async function submitRoomAction(
  code: string,
  round: RoomRound,
  action: RoomAction,
  songId?: number,
): Promise<{ state: RoomState; response: ActionResponse | null; recovered: boolean }> {
  try {
    const response = await roomRequest<ActionResponse>(`rooms/${code}/actions`, {
      position: round.position,
      expected_version: round.version,
      action,
      ...(songId === undefined ? {} : { song_id: songId }),
    });
    return { state: response.state, response, recovered: false };
  } catch (error) {
    if (error instanceof RoomApiError && error.status !== 409 && error.status < 500) throw error;
    const state = await roomRequest<RoomState>(`rooms/${code}/me`);
    return { state, response: null, recovered: true };
  }
}
