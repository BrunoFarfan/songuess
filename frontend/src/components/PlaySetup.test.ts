import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import Game from "./Game";
import { ChallengeDetails } from "./PlaySetup";
import type { RoomController } from "./useRoom";

describe("mode-first setup", () => {
  it("asks for solo or multiplayer before showing filters or join fields", () => {
    const html = renderToStaticMarkup(React.createElement(Game));
    expect(html).toContain("Choose play mode");
    expect(html).toContain("Solo");
    expect(html).toContain("Multiplayer");
    expect(html).not.toContain("Pick your mix.");
    expect(html).not.toContain("Challenge code");
    expect(html).not.toContain("Create challenge");
  });

  it.each([0, 21, 1.5])("does not allow creating a challenge with %s songs", (songCount) => {
    const room = {
      name: "Player",
      songCount,
      duration: 24,
      pending: false,
      validName: true,
      setMode: vi.fn(),
      setName: vi.fn(),
      setSongCount: vi.fn(),
      setDuration: vi.fn(),
      create: vi.fn(),
      error: "",
    } as unknown as RoomController;
    const filters = {
      genres: [],
      countries: [],
      artist: null,
      yearMin: 1960,
      yearMax: 2026,
      popularityMin: 0,
      popularityMax: 100,
    };
    const html = renderToStaticMarkup(React.createElement(ChallengeDetails, { room, filters }));
    expect(html).toMatch(/<button[^>]+disabled=""[^>]*>Create &amp; invite friends/);
  });
});
