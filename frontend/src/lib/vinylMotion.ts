/** Timing and poses shared by both directions of the record/sleeve handoff. */
export const UI_FADE_DURATION = 180;
export const NEEDLE_PARK_DURATION = 420;
export const NEEDLE_FADE_DURATION = 180;
export const REVEAL_DURATION = 2100;
export const RETURN_DURATION = 1500;

export type DeckPose = {
  recordX: number;
  recordY: number;
  recordScale: number;
  recordTilt: number;
  sleeveX: number;
  sleeveY: number;
  sleeveScale: number;
  sleeveTurn: number;
  sleeveRoll: number;
  assemblyX: number;
  assemblyY: number;
};

export function smooth(value: number) {
  const t = Math.min(Math.max(value, 0), 1);
  return t * t * (3 - 2 * t);
}
const mix = (a: number, b: number, t: number) => a + (b - a) * t;

export function revealPose(
  from: DeckPose,
  halfOffset: number,
  progress: number,
  entryDistance = 3.8,
): DeckPose {
  // Lift from the actual deck pose before the sleeve swivels into place. Keep
  // the pressing fully outside the mouth until both objects share its plane.
  const lift = smooth(progress / 0.58);
  const insertion = smooth((progress - 0.64) / 0.36);
  const swivel = smooth((progress - 0.16) / 0.43);
  const tilt = smooth(progress / 0.58);
  return {
    recordX: mix(from.recordX, halfOffset + 1.08, lift) - 1.08 * insertion,
    recordY: mix(from.recordY, 0, lift) + Math.sin(Math.PI * lift) * 0.16,
    recordScale: mix(from.recordScale, 1, lift),
    recordTilt: mix(from.recordTilt, 0, tilt),
    sleeveX: -halfOffset - (1 - swivel) * entryDistance,
    sleeveY: Math.sin(Math.PI * swivel) * 0.24,
    sleeveScale: 1,
    sleeveTurn: -Math.PI * 2 * (1 - swivel),
    sleeveRoll: -Math.sin(Math.PI * swivel) * 0.18,
    assemblyX: mix(from.assemblyX, 0.018, tilt),
    assemblyY: mix(from.assemblyY, -0.1, tilt),
  };
}

export function returnPose(
  from: DeckPose,
  to: DeckPose,
  progress: number,
  exitDistance = 4.5,
): DeckPose {
  // Extract along the cavity first. Only then turn/lower the record onto the
  // deck; rotating a partially inserted record would cut through the panels.
  const extraction = smooth(progress / 0.38);
  const placement = smooth((progress - 0.4) / 0.6);
  const departure = Math.min(Math.max((progress - 0.4) / 0.48, 0), 1);
  const exit = departure ** 3;
  return {
    recordX: mix(from.recordX + 1.12 * from.recordScale * extraction, to.recordX, placement),
    recordY: mix(from.recordY, to.recordY, placement) + Math.sin(Math.PI * placement) * 0.18,
    recordScale: mix(from.recordScale, to.recordScale, placement),
    recordTilt: mix(from.recordTilt, to.recordTilt, placement),
    sleeveX: from.sleeveX - exit * exitDistance,
    sleeveY: from.sleeveY - Math.sin(Math.PI * departure) * 0.12,
    sleeveScale: from.sleeveScale,
    sleeveTurn: from.sleeveTurn - departure * 0.45,
    sleeveRoll: from.sleeveRoll + Math.sin(Math.PI * departure) * 0.1,
    assemblyX: mix(from.assemblyX, 0, placement),
    assemblyY: mix(from.assemblyY, 0, placement),
  };
}
