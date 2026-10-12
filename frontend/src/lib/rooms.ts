import type { SetupFilters } from "../components/SetupWizard";
import {
  pointsForResult,
  type ClueNumber,
  type LocalRoundRecord,
  type RoundResult,
} from "./roundHistory";

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
  closes_at: string;
  playlist: { position: number; reveal: RoomReveal }[];
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
    player_id: string;
    display_name: string;
    rank: number;
    points: number;
    correct_count: number;
    completed_count: number;
    is_you: boolean;
    songs: { position: number; points: number | null; result: RoundResult | null }[];
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

export type PlaybackSnapshot = {
  state: RoomState;
  confirmedState: RoomState;
  result: RoomResult | null;
  uncertain: boolean;
  error: string;
};

type QueuedAction = { round: RoomRound; action: RoomAction; songId?: number; expected: RoomState };

// The queue is bounded by the frozen playlist and six clues per round. Every
// prediction consumes a clue or completes a round, including synchronous clicks.
export class RoomPlayback {
  snapshot: PlaybackSnapshot;
  private queue: QueuedAction[] = [];
  private saving = false;
  private disposed = false;
  private completion: Promise<void> = Promise.resolve();
  settled() {
    return this.completion;
  }
  get isSaving() {
    return this.saving;
  }
  constructor(
    state: RoomState,
    private changed: (snapshot: PlaybackSnapshot) => void,
  ) {
    this.snapshot = { state, confirmedState: state, result: null, uncertain: false, error: "" };
  }
  dispose() {
    this.disposed = true;
  }
  private emit() {
    if (!this.disposed) this.changed(this.snapshot);
  }
  next() {
    if (this.snapshot.uncertain || !this.snapshot.result) return;
    if (Date.now() >= Date.parse(this.snapshot.state.closes_at)) {
      this.snapshot = {
        ...this.snapshot,
        error: "This challenge has closed. Refresh the standings.",
      };
      this.emit();
      return;
    }
    this.snapshot = { ...this.snapshot, result: null };
    this.emit();
  }
  action(action: RoomAction, songId?: number, guess?: RoomSong) {
    const { state, result, uncertain } = this.snapshot;
    const round = state.current_round;
    const reveal = state.playlist.find((item) => item.position === round?.position)?.reveal;
    if (this.disposed || uncertain || result || !round || !reveal || state.closed) return;
    if (
      !Number.isFinite(Date.parse(state.closes_at)) ||
      Date.now() >= Date.parse(state.closes_at)
    ) {
      this.snapshot = {
        ...this.snapshot,
        error: "This challenge has closed. Refresh the standings.",
      };
      this.emit();
      return;
    }
    if (
      action === "guess" &&
      (songId === undefined || round.guesses.some((item) => item.id === songId))
    )
      return;
    const correct = action === "guess" && songId === reveal.id;
    const completed = correct || action === "give_up" || round.clue_number === 6;
    const guesses =
      action === "guess"
        ? [
            ...round.guesses,
            guess ??
              state.playlist.find((item) => item.reveal.id === songId)?.reveal ?? {
                id: songId!,
                title: "Previous guess",
                artist: "",
                album: null,
                release_year: 0,
                artwork_url: null,
                popularity_score: null,
              },
          ]
        : round.guesses;
    let predicted: RoomState;
    let predictedResult: RoomResult | null = null;
    if (completed) {
      predictedResult = {
        position: round.position,
        result: correct ? `clue_${round.clue_number as ClueNumber}` : "not_guessed",
        completed_at: new Date().toISOString(),
        reveal,
      };
      const next = state.playlist.find((item) => item.position === round.position + 1);
      predicted = {
        ...state,
        completed_count: state.completed_count + 1,
        total_points: state.total_points + pointsForResult(predictedResult.result),
        results: [...state.results, predictedResult],
        current_round: next
          ? {
              position: next.position,
              preview_url: next.reveal.preview_url,
              clue_number: 1,
              guesses: [],
              version: 0,
            }
          : null,
      };
    } else {
      predicted = {
        ...state,
        current_round: {
          ...round,
          guesses,
          clue_number: round.clue_number + 1,
          version: round.version + 1,
        },
      };
    }
    this.queue.push({ round, action, songId, expected: predicted });
    this.snapshot = { ...this.snapshot, state: predicted, result: predictedResult, error: "" };
    this.emit();
    if (!this.saving) this.completion = this.drain();
  }
  private reconcile(state: RoomState, position: number, message: string) {
    this.queue = [];
    this.snapshot = {
      state,
      confirmedState: state,
      result: state.results.find((item) => item.position === position) ?? null,
      uncertain: false,
      error: message,
    };
    this.emit();
  }
  private async drain() {
    if (this.saving) return;
    this.saving = true;
    try {
      while (this.queue.length) {
        const item = this.queue[0];
        try {
          const next = await submitRoomAction(
            this.snapshot.state.code,
            item.round,
            item.action,
            item.songId,
          );
          const actual = next.state.current_round;
          const expected = item.expected.current_round;
          const actualResult = next.state.results.find(
            (result) => result.position === item.round.position,
          )?.result;
          const expectedResult = item.expected.results.find(
            (result) => result.position === item.round.position,
          )?.result;
          const matches =
            actual?.position === expected?.position &&
            actual?.version === expected?.version &&
            actual?.clue_number === expected?.clue_number &&
            actualResult === expectedResult &&
            next.state.completed_count === item.expected.completed_count;
          if (next.recovered || next.state.closed || !matches) {
            this.reconcile(
              next.state,
              item.round.position,
              "Progress refreshed. Review the current clue before continuing.",
            );
            return;
          }
          this.queue.shift();
          this.snapshot = {
            ...this.snapshot,
            confirmedState: next.state,
            // Older acknowledgements never replace a later clue/song/reveal.
            state: this.queue.length ? this.snapshot.state : next.state,
            result: this.snapshot.result
              ? (next.state.results.find(
                  (result) => result.position === this.snapshot.result?.position,
                ) ?? this.snapshot.result)
              : null,
          };
          this.emit();
        } catch (error) {
          this.queue = [];
          // Definitive rejections also discard dependent predictions. Recovery
          // failures must block play until an explicit authoritative resume.
          if (error instanceof RoomApiError && error.status < 500 && error.status !== 409) {
            try {
              const state = await roomRequest<RoomState>(`rooms/${this.snapshot.state.code}/me`);
              this.reconcile(state, item.round.position, error.message);
              return;
            } catch {
              /* Block below if the reconciliation read fails. */
            }
          }
          this.snapshot = {
            ...this.snapshot,
            uncertain: true,
            error: error instanceof Error ? error.message : "Could not refresh progress.",
          };
          this.emit();
          return;
        }
      }
    } finally {
      this.saving = false;
    }
  }
}
