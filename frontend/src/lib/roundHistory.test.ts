import { describe, expect, it } from "vitest";

import {
  addOrUpdateRound,
  calculatePersonalStats,
  parseRoundHistory,
  pointsForResult,
  resultForRound,
  type LocalRoundRecord,
} from "./roundHistory";

function record(id: string, result: LocalRoundRecord["result"]): LocalRoundRecord {
  return { id, songId: Number(id), result, completedAt: `2026-08-19T00:00:0${id}.000Z` };
}

describe("shared round result model", () => {
  it("maps correct rounds to their clue and every failure to not guessed", () => {
    expect(resultForRound("correct", 0)).toBe("clue_1");
    expect(resultForRound("correct", 5)).toBe("clue_6");
    expect(resultForRound("failed", 5)).toBe("not_guessed");
    expect(resultForRound("gave_up", 2)).toBe("not_guessed");
  });

  it("uses the agreed six-to-zero point scale", () => {
    expect(pointsForResult("clue_1")).toBe(6);
    expect(pointsForResult("clue_6")).toBe(1);
    expect(pointsForResult("not_guessed")).toBe(0);
  });
});

describe("personal statistics", () => {
  it("derives percentages, clue distribution, averages, points, and streaks", () => {
    const history = [
      record("5", "clue_2"),
      record("4", "clue_1"),
      record("3", "not_guessed"),
      record("2", "clue_4"),
      record("1", "clue_3"),
    ];

    expect(calculatePersonalStats(history)).toEqual({
      totalSongs: 5,
      correctSongs: 4,
      notGuessedSongs: 1,
      correctPercentage: 80,
      notGuessedPercentage: 20,
      clueDistribution: { 1: 1, 2: 1, 3: 1, 4: 1, 5: 0, 6: 0 },
      averageClue: 2.5,
      currentStreak: 2,
      bestStreak: 2,
      totalPoints: 18,
    });
  });

  it("upserts the same completed round instead of double counting it", () => {
    const initial = addOrUpdateRound([], record("1", "clue_2"));
    const enriched = addOrUpdateRound(initial, { ...record("1", "clue_2"), title: "Song" });

    expect(enriched).toHaveLength(1);
    expect(enriched[0].title).toBe("Song");
  });

  it("ignores malformed browser storage", () => {
    expect(parseRoundHistory("not-json")).toEqual([]);
    expect(parseRoundHistory(JSON.stringify([{ result: "clue_1" }]))).toEqual([]);
  });
});
