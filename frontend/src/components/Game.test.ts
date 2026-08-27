import { describe, expect, it } from "vitest";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";

import AlbumGuessBrowser, {
  swipeLiveTraversalDelta,
  swipeTraversalDelta,
  visibleAlbumIndexes,
  wrapAlbumIndex,
  type AlbumGuessOption,
} from "./AlbumGuessBrowser";
import VinylSleeveReveal from "./VinylSleeveReveal";

import {
  countryFlag,
  createRoundRequest,
  excludeWrongGuess,
  PersonalStatistics,
  PotentialPoints,
  potentialPointsForAttempt,
  filterCountryOptions,
  type CountryOption,
} from "./Game";

describe("personal statistics history", () => {
  it("expands recent rounds by default", () => {
    const html = renderToStaticMarkup(
      createElement(PersonalStatistics, {
        history: [
          {
            id: "round-1",
            songId: 1,
            result: "clue_1" as const,
            completedAt: "2026-08-25T00:00:00.000Z",
            title: "Test Song",
            artist: "Test Artist",
          },
        ],
        stats: {
          totalSongs: 1,
          correctSongs: 1,
          notGuessedSongs: 0,
          correctPercentage: 100,
          notGuessedPercentage: 0,
          clueDistribution: { 1: 1, 2: 0, 3: 0, 4: 0, 5: 0, 6: 0 },
          averageClue: 1,
          currentStreak: 1,
          bestStreak: 1,
          totalPoints: 6,
        },
      }),
    );

    expect(html).toContain('<details class="round-history" open="">');
    expect(html).toContain("Test Song");
  });
});

describe("potential clue points", () => {
  it("derives available points from the existing attempt index", () => {
    expect(potentialPointsForAttempt(0)).toBe(6);
    expect(potentialPointsForAttempt(1)).toBe(5);
    expect(potentialPointsForAttempt(5)).toBe(1);
    expect(potentialPointsForAttempt(6)).toBe(0);
  });

  it("exposes the remaining score accessibly and marks the latest spent star", () => {
    const html = renderToStaticMarkup(createElement(PotentialPoints, { attempt: 2 }));

    expect(html).toContain('role="meter"');
    expect(html).toContain('aria-label="4 of 6 points available"');
    expect(html).toContain('aria-valuenow="4"');
    expect(html.match(/is-available/g)).toHaveLength(4);
    expect(html.match(/is-lost/g)).toHaveLength(1);
  });
});
import {
  clearDownstreamFilters,
  createFilterContextRequest,
  defaultFilters,
  formatSongCount,
  reconcileContextualFilters,
  resetStep,
  resolvePreset,
  SETUP_PRESETS,
  shouldShowSetupOverlay,
  toggleSetupOption,
  type ArtistOption,
  type SetupFilterMetadata,
} from "./SetupWizard";

const countries: CountryOption[] = [
  { code: "CL", name: "Chile", flag: "🇨🇱" },
  { code: "CN", name: "China", flag: "🇨🇳" },
  { code: "US", name: "United States", flag: "🇺🇸" },
];

describe("country origin lookup", () => {
  it("matches prefixes against full country names", () => {
    expect(filterCountryOptions(countries, "chi").map(({ code }) => code)).toEqual(["CL", "CN"]);
  });

  it("matches ISO country-code prefixes and ignores surrounding space", () => {
    expect(filterCountryOptions(countries, " us ").map(({ code }) => code)).toEqual(["US"]);
  });

  it("renders ISO country codes as flags", () => {
    expect(countryFlag("cl")).toBe("🇨🇱");
  });
});

describe("wrong guess carousel continuity", () => {
  const searchResults = [
    {
      id: 1,
      title: "First",
      artist: "Artist A",
      album: "Album A",
      release_year: 2001,
      artwork_url: null,
      popularity_score: 91,
    },
    {
      id: 2,
      title: "Second",
      artist: "Artist B",
      album: "Album B",
      release_year: 2002,
      artwork_url: "https://example.com/second.jpg",
      popularity_score: 72,
    },
    {
      id: 3,
      title: "Third",
      artist: "Artist C",
      album: null,
      release_year: 2003,
      artwork_url: null,
      popularity_score: 44,
    },
  ];

  it("removes only the wrong active option and activates its successor", () => {
    const result = excludeWrongGuess(searchResults, 2);

    expect(result.remainingResults.map(({ id }) => id)).toEqual([1, 3]);
    expect(result.nextGuess?.id).toBe(3);
  });

  it("wraps to the first remaining option after guessing the last one", () => {
    const result = excludeWrongGuess(searchResults, 3);

    expect(result.remainingResults.map(({ id }) => id)).toEqual([1, 2]);
    expect(result.nextGuess?.id).toBe(1);
  });
});

describe("song search carousel query boundaries", () => {
  const rankedResults: AlbumGuessOption[] = [
    {
      id: 1939,
      title: "Mr. Brightside",
      artist: "The Killers",
      album: "Hot Fuss",
      release_year: 2003,
      artwork_url: null,
      popularity_score: 87,
    },
    {
      id: 2190,
      title: "Might",
      artist: "Modest Mouse",
      album: "This Is a Long Drive",
      release_year: 1996,
      artwork_url: null,
      popularity_score: 35,
    },
  ];

  it("keeps the settled carousel and loading status visible during the next request", () => {
    const html = renderToStaticMarkup(
      createElement(AlbumGuessBrowser, {
        results: rankedResults,
        query: "the killers",
        isSearching: true,
        onSelect: () => undefined,
      }),
    );

    expect(html).toContain("Mr. Brightside");
    expect(html).toContain("Popularity score: 87 out of 100");
    expect(html).toContain("Pulling records…");
  });

  it("marks a missing popularity snapshot as unavailable instead of zero", () => {
    const html = renderToStaticMarkup(
      createElement(AlbumGuessBrowser, {
        results: [{ ...rankedResults[0], popularity_score: null }],
        onSelect: () => undefined,
      }),
    );

    expect(html).toContain("Popularity score unavailable");
    expect(html).toContain("N/A");
  });
});

describe("revealed song popularity", () => {
  it("shows the score alongside the guessed song details", () => {
    const html = renderToStaticMarkup(
      createElement(VinylSleeveReveal, {
        revealed: true,
        outcome: "correct",
        song: {
          title: "15 Step",
          artist: "Radiohead",
          album: "In Rainbows",
          release_year: 2007,
          artwork_url: null,
          popularity_score: 76,
          genres: ["alternative"],
          apple_music_url: "https://music.apple.com/us/album/15-step/1?i=2",
          spotify_url: "https://open.spotify.com/track/0VjIjW4GlUZAMYd2vXMi3b",
        },
        children: createElement("span", null, "Record"),
      }),
    );

    expect(html).toContain("Popularity score: 76 out of 100");
    expect(html).toContain("Listen on Apple Music");
    expect(html).toContain("Listen on Spotify");
    expect(html).toContain('target="_blank"');
    expect(html).toContain('rel="noopener noreferrer"');
    expect(html.match(/vinyl-sleeve-reveal__confetti/g)).toHaveLength(1);
    expect(html).toMatch(
      /<div class="vinyl-sleeve-reveal__confetti" aria-hidden="true">(?:<span><\/span>){14}<\/div>/,
    );
  });

  it("does not invent a Spotify destination when only Apple Music is exact", () => {
    const html = renderToStaticMarkup(
      createElement(VinylSleeveReveal, {
        revealed: true,
        outcome: "correct",
        song: {
          title: "15 Step",
          artist: "Radiohead",
          popularity_score: 76,
          apple_music_url: "https://music.apple.com/us/album/15-step/1?i=2",
          spotify_url: null,
        },
        children: createElement("span", null, "Record"),
      }),
    );

    expect(html).toContain("Listen on Apple Music");
    expect(html).not.toContain("Listen on Spotify");
  });
});

describe("mobile album shelf gestures", () => {
  it("wraps against the complete result count instead of a loaded page", () => {
    expect(wrapAlbumIndex(-1, 80)).toBe(79);
    expect(wrapAlbumIndex(80, 80)).toBe(0);
    expect(visibleAlbumIndexes(0, 80)).toEqual([75, 76, 77, 78, 79, 0, 1, 2, 3, 4, 5]);
    expect(visibleAlbumIndexes(79, 80)).toEqual([74, 75, 76, 77, 78, 79, 0, 1, 2, 3, 4]);
  });

  it("keeps the same absolute slots when another page arrives", () => {
    expect(visibleAlbumIndexes(37, 80)).toEqual([32, 33, 34, 35, 36, 37, 38, 39, 40, 41, 42]);
    expect(visibleAlbumIndexes(37, 120)).toEqual([32, 33, 34, 35, 36, 37, 38, 39, 40, 41, 42]);
  });

  it("skips previously guessed absolute slots without breaking the ring", () => {
    expect(visibleAlbumIndexes(0, 12, new Set([11, 1]))).toEqual([7, 8, 9, 10, 0, 2, 3, 4, 5, 6]);
  });

  it("ignores taps and tiny drags", () => {
    expect(swipeTraversalDelta({ distance: 4, elapsed: 120 })).toBe(0);
  });

  it("moves proportionally through several records on one long swipe", () => {
    expect(swipeTraversalDelta({ distance: -180, elapsed: 420 })).toBe(3);
    expect(swipeTraversalDelta({ distance: 180, elapsed: 420 })).toBe(-3);
  });

  it("projects a quick flick while bounding traversal", () => {
    expect(swipeTraversalDelta({ distance: -34, elapsed: 40 })).toBe(1);
    expect(swipeTraversalDelta({ distance: -80, elapsed: 50 })).toBe(3);
    expect(swipeTraversalDelta({ distance: -600, elapsed: 100 })).toBe(7);
  });

  it("crosses record thresholds while the finger is still moving", () => {
    expect(swipeLiveTraversalDelta(-30)).toBe(0);
    expect(swipeLiveTraversalDelta(-70)).toBe(1);
    expect(swipeLiveTraversalDelta(-140)).toBe(2);
    expect(swipeLiveTraversalDelta(140)).toBe(-2);
  });
});

const metadata: SetupFilterMetadata = {
  genres: ["pop"],
  countries: ["US"],
  year_min: 1950,
  year_max: 2026,
  popularity_min: 0,
  popularity_max: 100,
  song_count: 20,
};

const theWeeknd: ArtistOption = {
  id: "c8b03190-306c-4120-bb0b-6f2ebfc06ea9",
  name: "The Weeknd",
  disambiguation: null,
  song_count: 31,
};

describe("artist identity filter", () => {
  it("sends one stable artist identifier in the round request", () => {
    const filters = { ...defaultFilters(metadata), artist: theWeeknd };

    expect(createRoundRequest(filters, [12])).toMatchObject({
      artist_id: theWeeknd.id,
      exclude_ids: [12],
    });
    expect(createRoundRequest(filters, [])).not.toHaveProperty("artists");
  });

  it("uses no artist for custom defaults and presets", () => {
    expect(defaultFilters(metadata).artist).toBeNull();
    expect(
      resolvePreset(
        {
          id: "pop",
          title: "Pop",
          subtitle: "Pop songs",
          genres: ["pop"],
        },
        metadata,
      ).artist,
    ).toBeNull();
  });

  it("clears the selected artist without changing the other filters", () => {
    const filters = { ...defaultFilters(metadata), artist: theWeeknd, genres: ["pop"] };

    expect(resetStep(filters, "artist", defaultFilters(metadata))).toEqual({
      ...filters,
      artist: null,
    });
  });
});

describe("progressive setup availability", () => {
  it("clears every downstream selection when an earlier filter changes", () => {
    const defaults = defaultFilters(metadata);
    const selected = {
      ...defaults,
      artist: theWeeknd,
      genres: ["pop"],
      countries: ["US"],
      yearMin: 2000,
      popularityMin: 50,
    };

    expect(clearDownstreamFilters(selected, "genre", defaults)).toEqual({
      ...selected,
      countries: [],
      yearMin: defaults.yearMin,
      yearMax: defaults.yearMax,
      popularityMin: defaults.popularityMin,
      popularityMax: defaults.popularityMax,
    });
  });

  it("removes unavailable values and clamps ranges to contextual bounds", () => {
    const defaults = defaultFilters(metadata);
    const filters = {
      ...defaults,
      genres: ["pop", "metal"],
      countries: ["US", "GB"],
      yearMin: 1950,
      yearMax: 2026,
    };
    const context: SetupFilterMetadata = {
      genres: ["pop"],
      countries: ["US"],
      year_min: 1999,
      year_max: 2018,
      popularity_min: 27,
      popularity_max: 95,
      song_count: 4,
    };

    expect(reconcileContextualFilters(filters, context, defaults)).toMatchObject({
      genres: ["pop"],
      countries: ["US"],
      yearMin: 1999,
      yearMax: 2018,
      popularityMin: 0,
      popularityMax: 100,
    });
  });

  it("sends the normalized singular artist and current selections to context", () => {
    const filters = { ...defaultFilters(metadata), artist: theWeeknd, genres: ["pop"] };
    expect(createFilterContextRequest(filters)).toMatchObject({
      artist_id: theWeeknd.id,
      genres: ["pop"],
      countries: [],
    });
  });
});

describe("curated preset semantics", () => {
  it("defines exact filters for each curated pressing", () => {
    expect(SETUP_PRESETS).toEqual([
      { id: "random", title: "Random", subtitle: "" },
      {
        id: "pop-this-century",
        title: "Pop This Century",
        subtitle: "Hooks from 2000 onward",
        genres: ["pop"],
        year: [2000, new Date().getFullYear()],
        popularity: [80, 100],
      },
      {
        id: "classical-essentials",
        title: "Classical Essentials",
        subtitle: "Composers and cornerstone works",
        genres: ["classical"],
        popularity: [80, 100],
      },
      {
        id: "80s-greatest-hits",
        title: "80s Greatest Hits",
        subtitle: "Big choruses, bigger production",
        year: [1980, 1989],
        popularity: [80, 100],
      },
      {
        id: "90s-deep-cuts",
        title: "90s Deep Cuts",
        subtitle: "Beyond the obvious singles",
        genres: ["alternative", "alternative rock"],
        year: [1990, 1999],
        popularity: [20, 55],
      },
      {
        id: "golden-oldies",
        title: "Golden Oldies",
        subtitle: "Three decades of standards",
        year: [1950, 1979],
        popularity: [80, 100],
      },
      {
        id: "dancefloor",
        title: "Dancefloor",
        subtitle: "Disco, dance and electronic pulse",
        genres: ["dance", "disco", "electronic"],
        popularity: [75, 100],
      },
      {
        id: "hip-hop-heavyweights",
        title: "Hip-Hop Heavyweights",
        subtitle: "Rap's defining voices",
        genres: ["hip hop", "hip-hop", "rap"],
        popularity: [80, 100],
      },
      {
        id: "latin-party",
        title: "Latin Party",
        subtitle: "A cross-continent celebration",
        countries: ["AR", "BR", "CL", "CO", "MX", "PE", "PR"],
        popularity: [80, 100],
      },
      {
        id: "rock-anthems",
        title: "Rock Anthems",
        subtitle: "Riffs built for the rafters",
        genres: ["rock"],
        popularity: [80, 100],
      },
    ]);
  });

  it("resolves Random to the complete catalog defaults", () => {
    expect(resolvePreset(SETUP_PRESETS[0], metadata)).toEqual(defaultFilters(metadata));
  });

  it("formats the resolved pool count with correct plurality", () => {
    expect(formatSongCount(1)).toBe("1 song");
    expect(formatSongCount(2)).toBe("2 songs");
  });

  it("preserves authored bounds when catalog metadata reports narrower observed values", () => {
    const preset = resolvePreset(
      SETUP_PRESETS.find(({ id }) => id === "hip-hop-heavyweights")!,
      metadata,
    );
    const hipHopContext = {
      ...metadata,
      popularity_min: 73,
      popularity_max: 95,
    };

    expect(
      reconcileContextualFilters(preset, hipHopContext, defaultFilters(metadata)),
    ).toMatchObject({
      popularityMin: 80,
      popularityMax: 100,
    });
  });

  it("does not clamp authored preset bounds while resolving against catalog metadata", () => {
    const narrowMetadata = { ...metadata, popularity_min: 40, popularity_max: 55 };
    const hits = SETUP_PRESETS.find(({ id }) => id === "rock-anthems")!;
    const deepCuts = SETUP_PRESETS.find(({ id }) => id === "90s-deep-cuts")!;

    expect(resolvePreset(hits, narrowMetadata)).toMatchObject({
      popularityMin: 80,
      popularityMax: 100,
    });
    expect(resolvePreset(deepCuts, narrowMetadata)).toMatchObject({
      popularityMin: 20,
      popularityMax: 55,
    });
  });
});

describe("setup search overlay visibility", () => {
  it("opens artist results only for an active query with matches", () => {
    expect(shouldShowSetupOverlay(true, "week", 3)).toBe(true);
    expect(shouldShowSetupOverlay(false, "week", 3)).toBe(false);
    expect(shouldShowSetupOverlay(true, "   ", 3)).toBe(false);
    expect(shouldShowSetupOverlay(true, "week", 0)).toBe(false);
  });

  it("keeps multi-select values stable while adding and removing options", () => {
    expect(toggleSetupOption(["alternative"], "dance")).toEqual(["alternative", "dance"]);
    expect(toggleSetupOption(["alternative", "dance"], "alternative")).toEqual(["dance"]);
  });
});
