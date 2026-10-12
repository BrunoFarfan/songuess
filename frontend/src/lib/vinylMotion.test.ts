import { describe, expect, it } from "vitest";
import { revealPose, returnPose, type DeckPose } from "./vinylMotion";

const playing: DeckPose = {
  recordX: -0.2,
  recordY: 0.1,
  recordScale: 1.4,
  recordTilt: -0.07,
  sleeveX: -5,
  sleeveY: 0,
  sleeveScale: 1,
  sleeveTurn: 0,
  sleeveRoll: 0,
  assemblyX: 0,
  assemblyY: 0,
};

describe("connected vinyl motion", () => {
  it("starts a wide-screen sleeve offscreen without changing its spin or record path", () => {
    const start = revealPose(playing, 0.5, 0, 16);
    expect(start.sleeveX + Math.SQRT2).toBeLessThan(-12);
    const extended = revealPose(playing, 0.5, 0.4, 16);
    const original = revealPose(playing, 0.5, 0.4);
    expect(extended.sleeveTurn).toBe(original.sleeveTurn);
    expect(extended.recordX).toBe(original.recordX);
    expect(revealPose(playing, 0.5, 1, 16)).toEqual(revealPose(playing, 0.5, 1));
  });
  it("accelerates a longer desktop sleeve exit without changing record placement", () => {
    const revealed = revealPose(playing, 0.5, 1);
    const early = returnPose(revealed, playing, 0.56, 18);
    const late = returnPose(revealed, playing, 0.72, 18);
    expect(revealed.sleeveX - late.sleeveX).toBeGreaterThan(4 * (revealed.sleeveX - early.sleeveX));
    const end = returnPose(revealed, playing, 1, 18);
    expect(end.sleeveX).toBeLessThan(-18);
    expect(end.recordX).toBeCloseTo(playing.recordX);
    expect(end.recordScale).toBeCloseTo(playing.recordScale);
  });
  it("starts reveal at the playing record's exact size, position and tilt", () => {
    const start = revealPose(playing, 0.5, 0);
    for (const key of [
      "recordX",
      "recordY",
      "recordScale",
      "recordTilt",
      "assemblyX",
      "assemblyY",
    ] as const)
      expect(start[key]).toBe(playing[key]);
  });

  it("aligns and resizes the record before it enters the sleeve cavity", () => {
    for (let step = 0; step <= 100; step++) {
      const pose = revealPose(playing, 0.5, step / 100);
      if (pose.recordX - pose.sleeveX < 1.94) {
        expect(pose.recordScale).toBe(1);
        expect(pose.recordTilt).toBe(0);
        expect(pose.recordY).toBeCloseTo(pose.sleeveY);
        expect(Math.abs(pose.sleeveTurn)).toBeLessThan(0.000001);
      }
    }
  });

  it("starts extraction at the exact final reveal pose", () => {
    const revealed = revealPose(playing, 0.5, 1);
    expect(returnPose(revealed, playing, 0)).toEqual(revealed);
  });

  it("keeps the record aligned until it is fully outside, then returns to its play pose", () => {
    const revealed = revealPose(playing, 0.5, 1);
    const extracted = returnPose(revealed, playing, 0.38);
    expect(extracted.recordX - extracted.sleeveX).toBeGreaterThan(1.94);
    expect(extracted.recordScale).toBe(1);
    expect(extracted.recordTilt).toBe(0);
    const end = returnPose(revealed, playing, 1);
    for (const key of [
      "recordX",
      "recordY",
      "recordScale",
      "recordTilt",
      "assemblyX",
      "assemblyY",
    ] as const)
      expect(end[key]).toBeCloseTo(playing[key]);
    expect(end.sleeveX).toBeLessThan(-4);
  });
});
