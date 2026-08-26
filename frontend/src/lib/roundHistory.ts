export const ROUND_HISTORY_STORAGE_KEY = "songuess.round-history.v1";
export const MAX_ROUND_HISTORY = 200;

export type ClueNumber = 1 | 2 | 3 | 4 | 5 | 6;
export type RoundResult = `clue_${ClueNumber}` | "not_guessed";

export type LocalRoundRecord = {
  id: string;
  songId: number;
  result: RoundResult;
  completedAt: string;
  title?: string;
  artist?: string;
  artworkUrl?: string | null;
};

export type PersonalStats = {
  totalSongs: number;
  correctSongs: number;
  notGuessedSongs: number;
  correctPercentage: number;
  notGuessedPercentage: number;
  clueDistribution: Record<ClueNumber, number>;
  averageClue: number | null;
  currentStreak: number;
  bestStreak: number;
  totalPoints: number;
};

const clueNumbers: ClueNumber[] = [1, 2, 3, 4, 5, 6];

export function resultForRound(
  outcome: "correct" | "failed" | "gave_up",
  attempt: number,
): RoundResult {
  if (outcome !== "correct") return "not_guessed";
  return `clue_${Math.min(6, Math.max(1, attempt + 1)) as ClueNumber}`;
}

export function clueForResult(result: RoundResult): ClueNumber | null {
  if (result === "not_guessed") return null;
  return Number(result.slice(-1)) as ClueNumber;
}

export function pointsForResult(result: RoundResult): number {
  const clue = clueForResult(result);
  return clue === null ? 0 : 7 - clue;
}

export function addOrUpdateRound(
  history: LocalRoundRecord[],
  record: LocalRoundRecord,
): LocalRoundRecord[] {
  return [record, ...history.filter((item) => item.id !== record.id)].slice(0, MAX_ROUND_HISTORY);
}

export function calculatePersonalStats(history: LocalRoundRecord[]): PersonalStats {
  const clueDistribution = Object.fromEntries(clueNumbers.map((clue) => [clue, 0])) as Record<
    ClueNumber,
    number
  >;
  let correctSongs = 0;
  let clueTotal = 0;
  let totalPoints = 0;

  for (const record of history) {
    const clue = clueForResult(record.result);
    totalPoints += pointsForResult(record.result);
    if (clue !== null) {
      correctSongs += 1;
      clueTotal += clue;
      clueDistribution[clue] += 1;
    }
  }

  let currentStreak = 0;
  for (const record of history) {
    if (record.result === "not_guessed") break;
    currentStreak += 1;
  }

  let bestStreak = 0;
  let runningStreak = 0;
  for (const record of [...history].reverse()) {
    if (record.result === "not_guessed") {
      runningStreak = 0;
    } else {
      runningStreak += 1;
      bestStreak = Math.max(bestStreak, runningStreak);
    }
  }

  const totalSongs = history.length;
  const notGuessedSongs = totalSongs - correctSongs;
  return {
    totalSongs,
    correctSongs,
    notGuessedSongs,
    correctPercentage: totalSongs ? Math.round((correctSongs / totalSongs) * 100) : 0,
    notGuessedPercentage: totalSongs ? Math.round((notGuessedSongs / totalSongs) * 100) : 0,
    clueDistribution,
    averageClue: correctSongs ? clueTotal / correctSongs : null,
    currentStreak,
    bestStreak,
    totalPoints,
  };
}

export function parseRoundHistory(raw: string | null): LocalRoundRecord[] {
  if (!raw) return [];
  try {
    const value: unknown = JSON.parse(raw);
    if (!Array.isArray(value)) return [];
    return value.filter(isLocalRoundRecord).slice(0, MAX_ROUND_HISTORY);
  } catch {
    return [];
  }
}

export function loadRoundHistory(): LocalRoundRecord[] {
  if (typeof window === "undefined") return [];
  try {
    return parseRoundHistory(window.localStorage.getItem(ROUND_HISTORY_STORAGE_KEY));
  } catch {
    return [];
  }
}

export function saveRoundHistory(history: LocalRoundRecord[]): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(ROUND_HISTORY_STORAGE_KEY, JSON.stringify(history));
  } catch {
    // Storage may be unavailable in private browsing or constrained embeds.
  }
}

function isLocalRoundRecord(value: unknown): value is LocalRoundRecord {
  if (typeof value !== "object" || value === null) return false;
  const candidate = value as Partial<LocalRoundRecord>;
  return (
    typeof candidate.id === "string" &&
    typeof candidate.songId === "number" &&
    typeof candidate.completedAt === "string" &&
    (candidate.result === "not_guessed" ||
      clueNumbers.some((clue) => candidate.result === `clue_${clue}`))
  );
}
