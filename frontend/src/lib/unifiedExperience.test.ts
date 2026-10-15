import { describe, expect, it } from "vitest";

const sources = Object.entries(
  import.meta.glob<string>(["../**/*.{ts,tsx,css,astro}", "!../**/*.test.{ts,tsx}"], {
    eager: true,
    query: "?raw",
    import: "default",
  }),
);

describe("one full visual experience", () => {
  it("does not select animations or scenes using reduced-motion or reduced-data preferences", () => {
    expect(sources.length).toBeGreaterThan(0);
    const violations = sources
      .filter(([, source]) => /prefers-reduced-(?:motion|data)/i.test(source))
      .map(([path]) => path);
    expect(violations).toEqual([]);
  });

  it("does not select a visual tier using data-saving, connection, or hardware signals", () => {
    const signals =
      /\b(?:saveData|effectiveType|deviceMemory|hardwareConcurrency)\b|navigator\s*(?:\?\.|\.)\s*(?:connection|mozConnection|webkitConnection)\b/i;
    const violations = sources.filter(([, source]) => signals.test(source)).map(([path]) => path);
    expect(violations).toEqual([]);
  });
});
