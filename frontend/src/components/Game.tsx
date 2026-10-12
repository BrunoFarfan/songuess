import React, {
  type CSSProperties,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";

import {
  addOrUpdateRound,
  calculatePersonalStats,
  loadRoundHistory,
  pointsForResult,
  resultForRound,
  saveRoundHistory,
  type LocalRoundRecord,
  type PersonalStats,
} from "../lib/roundHistory";
import { PrefetchCache } from "../lib/prefetchCache";
import AlbumGuessBrowser from "./AlbumGuessBrowser";
import SetupWizard, { type ArtistOption, type SetupFilters } from "./SetupWizard";
import Tutorial, { hasSeenTutorial } from "./Tutorial";
import VinylSleeveReveal from "./VinylSleeveReveal";
import "./VinylControls.css";
import ChallengeRoom from "./ChallengeRoom";
import {
  ModeSelection,
  MultiplayerMenu,
  JoinChallenge,
  ChallengeDetails,
  SetupNavigation,
} from "./PlaySetup";
import { useRoom } from "./useRoom";
import { roomHistory } from "../lib/rooms";

const SNIPPET_DURATIONS = [1, 2, 4, 7, 11, 15] as const;
const CURRENT_YEAR = new Date().getFullYear();
const SEARCH_PAGE_SIZE = 40;
const ROUND_PREFETCH_COUNT = 1;

export function shouldSearchSongs(query: string): boolean {
  return query.trim().length === 0 || query.trim().length >= 2;
}
export const DEFAULT_VOLUME = 0.5;
const VOLUME_SESSION_STORAGE_KEY = "songuess-volume";

export function potentialPointsForAttempt(attempt: number): number {
  return Math.max(0, SNIPPET_DURATIONS.length - Math.max(0, attempt));
}

export function PotentialPoints({ attempt }: { attempt: number }) {
  const availablePoints = potentialPointsForAttempt(attempt);

  return (
    <div
      className="potential-points"
      role="meter"
      aria-label={`${availablePoints} of ${SNIPPET_DURATIONS.length} points available`}
      aria-valuemin={0}
      aria-valuemax={SNIPPET_DURATIONS.length}
      aria-valuenow={availablePoints}
    >
      {SNIPPET_DURATIONS.map((_, index) => {
        const active = index < availablePoints;
        const justLost = attempt > 0 && index === availablePoints;
        return (
          <span
            aria-hidden="true"
            className={`${active ? "is-available" : "is-spent"}${justLost ? " is-lost" : ""}`}
            key={index}
          >
            ★
          </span>
        );
      })}
    </div>
  );
}

type Phase = "setup" | "loading" | "playing" | "revealed";
type RoundOutcome = "correct" | "failed" | "gave_up";

type Filters = {
  genres: string[];
  countries: string[];
  artist: ArtistOption | null;
  yearMin: number;
  yearMax: number;
  popularityMin: number;
  popularityMax: number;
};

type FilterMetadata = {
  genres: string[];
  countries: string[];
  year_min: number | null;
  year_max: number | null;
  popularity_min: number;
  popularity_max: number;
  song_count: number;
};

export type RoundResponse = {
  song_id: number;
  preview_url: string;
};

type MutableRef<T> = { current: T };

export function shouldAutoOpenTutorial(
  phase: Phase,
  hasCheckedTutorial: boolean,
  tutorialPending: boolean,
): boolean {
  return phase === "playing" && hasCheckedTutorial && tutorialPending;
}

export async function fillRoundPrefetchQueue(
  queueRef: MutableRef<RoundResponse[]>,
  excludedIdsRef: MutableRef<number[]>,
  requestRound: (excludedIds: number[]) => Promise<RoundResponse | null>,
  onExclusionsChange: (excludedIds: number[]) => void = () => undefined,
  targetSize = ROUND_PREFETCH_COUNT,
): Promise<void> {
  while (queueRef.current.length < targetSize) {
    const round = await requestRound([...excludedIdsRef.current]);
    if (!round || excludedIdsRef.current.includes(round.song_id)) return;

    queueRef.current = [...queueRef.current, round];
    excludedIdsRef.current = [...excludedIdsRef.current, round.song_id];
    onExclusionsChange(excludedIdsRef.current);
  }
}

type SearchResult = {
  id: number;
  title: string;
  artist: string;
  album: string | null;
  release_year: number;
  artwork_url: string | null;
  popularity_score: number | null;
  searchIndex?: number;
};

type SearchResponse = {
  items: SearchResult[];
  offset: number;
  limit: number;
  total: number;
  has_more: boolean;
};

export function excludeWrongGuess(
  results: SearchResult[],
  guessedId: number,
): { remainingResults: SearchResult[]; nextGuess: SearchResult | null } {
  const guessedIndex = results.findIndex((result) => result.id === guessedId);
  if (guessedIndex < 0) return { remainingResults: results, nextGuess: results[0] ?? null };

  const remainingResults = results.filter((result) => result.id !== guessedId);
  const nextGuess =
    remainingResults.length > 0 ? remainingResults[guessedIndex % remainingResults.length] : null;
  return { remainingResults, nextGuess };
}

type RevealedSong = SearchResult & {
  artwork_url: string | null;
  genres: string[];
  preview_url: string;
  apple_music_url: string | null;
  spotify_url: string | null;
};

const searchPageCache = new PrefetchCache<SearchResponse>();
const revealCache = new PrefetchCache<RevealedSong>(4);

async function requestSearchPage(
  offset: number,
  query: string,
  seed: number,
): Promise<SearchResponse> {
  const params = new URLSearchParams({ offset: String(offset), limit: String(SEARCH_PAGE_SIZE) });
  if (query) params.set("q", query);
  else params.set("seed", String(seed));
  const url = `/api/songs/search?${params}`;
  return searchPageCache.get(url, async () => {
    const response = await fetch(url);
    if (!response.ok) throw new Error("Search failed");
    return response.json();
  });
}

function requestReveal(songId: number): Promise<RevealedSong> {
  return revealCache.get(String(songId), async () => {
    const response = await fetch(`/api/songs/${songId}`);
    if (!response.ok) throw new Error(await readApiError(response));
    return response.json();
  });
}

async function prefetchCarousel() {
  // Warm scene code without speculative catalog page reads.
  await Promise.all([import("../lib/sceneAssets"), import("./AlbumCarouselScene")]);
}

const defaultFilters: Filters = {
  genres: [],
  countries: [],
  artist: null,
  yearMin: 1960,
  yearMax: CURRENT_YEAR,
  popularityMin: 0,
  popularityMax: 100,
};

export default function Game() {
  const room = useRoom();
  const isRoomPlaying = room.mode === "playing";
  const [phase, setPhase] = useState<Phase>("setup");
  const [metadata, setMetadata] = useState<FilterMetadata | null>(null);
  const [draftFilters, setDraftFilters] = useState<Filters>(defaultFilters);
  const [activeFilters, setActiveFilters] = useState<Filters>(defaultFilters);
  const [currentSongId, setCurrentSongId] = useState<number | null>(null);
  const [previewUrl, setPreviewUrl] = useState("");
  const [attempt, setAttempt] = useState(0);
  const [playbackCursor, setPlaybackCursor] = useState(0);
  const [previousGuesses, setPreviousGuesses] = useState<SearchResult[]>([]);
  const [outcome, setOutcome] = useState<RoundOutcome | null>(null);
  const [revealedSong, setRevealedSong] = useState<RevealedSong | null>(null);
  const [preparedReveal, setPreparedReveal] = useState<{ id: number; song: RevealedSong } | null>(
    null,
  );
  const [isRevealLoading, setIsRevealLoading] = useState(false);
  const [isDrawerOpen, setIsDrawerOpen] = useState(false);
  const [isAudioPlaying, setIsAudioPlaying] = useState(false);
  const [volume, setVolume] = useState(DEFAULT_VOLUME);
  const [audioError, setAudioError] = useState("");
  const [appError, setAppError] = useState("");
  const [query, setQuery] = useState("");
  const [searchResults, setSearchResults] = useState<SearchResult[]>([]);
  const [searchResultsQuery, setSearchResultsQuery] = useState("");
  const [selectedGuess, setSelectedGuess] = useState<SearchResult | null>(null);
  const [isSearching, setIsSearching] = useState(false);
  const [isLoadingMoreResults, setIsLoadingMoreResults] = useState(false);
  const [searchTotalCount, setSearchTotalCount] = useState(0);
  const [excludedSearchIndexes, setExcludedSearchIndexes] = useState<number[]>([]);
  const [roundHistory, setRoundHistory] = useState<LocalRoundRecord[]>([]);
  const [openUtilityPanel, setOpenUtilityPanel] = useState<"volume" | "stats" | null>(null);
  const [wrongFeedbackKey, setWrongFeedbackKey] = useState(0);
  const [isTutorialOpen, setIsTutorialOpen] = useState(false);
  const [hasCheckedTutorial, setHasCheckedTutorial] = useState(false);
  const [isTutorialPending, setIsTutorialPending] = useState(false);

  const audioRef = useRef<HTMLAudioElement>(null);
  const drawerRef = useRef<HTMLDialogElement>(null);
  const desktopUtilityRef = useRef<HTMLElement>(null);
  const drawerTriggerRef = useRef<HTMLButtonElement>(null);
  const browseSeedRef = useRef(0);
  const utilityTriggerRef = useRef<HTMLButtonElement | null>(null);
  const roundGenerationRef = useRef(0);
  const playlistGenerationRef = useRef(0);
  const activeFiltersRef = useRef<Filters>(defaultFilters);
  const excludedSongIdsRef = useRef<number[]>([]);
  const prefetchedRoundsRef = useRef<RoundResponse[]>([]);
  const prefetchPromiseRef = useRef<Promise<void> | null>(null);
  const prefetchControllerRef = useRef<AbortController | null>(null);
  const isAdvancingRoundRef = useRef(false);
  const recordedGenerationRef = useRef<number | null>(null);
  const roundHistoryRef = useRef<LocalRoundRecord[]>([]);
  const previousGuessesRef = useRef<SearchResult[]>([]);
  const searchGenerationRef = useRef(0);
  const loadedSearchPagesRef = useRef(new Set<number>());
  const pendingSearchPagesRef = useRef(new Set<number>());
  const searchTotalCountRef = useRef(0);
  const wrongFeedbackTimerRef = useRef<number | null>(null);
  const activeRoomRoundRef = useRef<string | null>(null);
  const activeRoomRevealRef = useRef<string | null>(null);
  const prefetchedAudioRef = useRef<HTMLAudioElement | null>(null);
  const roomPlaybackRef = useRef<"clue" | "reveal" | null>(null);
  const unlockedDuration = SNIPPET_DURATIONS[attempt];
  const personalStats = useMemo(() => calculatePersonalStats(roundHistory), [roundHistory]);

  useEffect(() => {
    if (!isRoomPlaying || !room.state) {
      activeRoomRoundRef.current = null;
      activeRoomRevealRef.current = null;
      roomPlaybackRef.current = null;
      return;
    }
    const current = room.state.current_round;
    if (room.result) {
      const revealKey = `${room.state.code}:${room.state.guest_id}:${room.result.position}:${room.result.result}`;
      if (activeRoomRevealRef.current !== revealKey) {
        stopAudio(true);
        roomPlaybackRef.current = "reveal";
        activeRoomRevealRef.current = revealKey;
      }
      setAttempt(
        room.result.result === "not_guessed" ? 5 : Number(room.result.result.slice(-1)) - 1,
      );
      setCurrentSongId(null);
      setPreviewUrl(room.result.reveal.preview_url);
      setRevealedSong(room.result.reveal);
      setPreparedReveal(null);
      setOutcome(room.result.result === "not_guessed" ? "failed" : "correct");
      setPhase("revealed");
    } else if (current) {
      activeRoomRevealRef.current = null;
      const roundKey = `${room.state.code}:${room.state.guest_id}:${current.position}`;
      if (activeRoomRoundRef.current !== roundKey) {
        stopAudio(true);
        resetRoundUi();
        activeRoomRoundRef.current = roundKey;
      } else {
        const newGuesses = current.guesses.filter(
          (guess) => !previousGuessesRef.current.some((previous) => previous.id === guess.id),
        );
        if (newGuesses.length) {
          showWrongFeedback();
          const guessedIds = new Set(newGuesses.map((guess) => guess.id));
          const excludedIndexes = searchResults.flatMap((result) =>
            guessedIds.has(result.id) && result.searchIndex !== undefined
              ? [result.searchIndex]
              : [],
          );
          const remainingResults = searchResults.filter((result) => !guessedIds.has(result.id));
          setSearchResults(remainingResults);
          setExcludedSearchIndexes((indexes) => [...indexes, ...excludedIndexes]);
          if (selectedGuess && guessedIds.has(selectedGuess.id)) {
            setSelectedGuess(excludeWrongGuess(searchResults, selectedGuess.id).nextGuess);
          }
        }
      }
      const cachedSong = room.state.playlist.find(
        (song) => song.position === current.position,
      )?.reveal;
      setCurrentSongId(cachedSong?.id ?? null);
      setPreparedReveal(cachedSong ? { id: cachedSong.id, song: cachedSong } : null);
      setPreviewUrl(current.preview_url);
      setAttempt(current.clue_number - 1);
      previousGuessesRef.current = current.guesses;
      setPreviousGuesses(current.guesses);
      setPhase("playing");
    }
  }, [isRoomPlaying, room.state, room.result]);

  useEffect(() => {
    if (!room.confirmedState) return;
    for (const record of roomHistory(room.confirmedState)) persistRoundHistory(record);
  }, [room.confirmedState]);

  useEffect(() => {
    if (room.mode !== "playing") {
      stopAudio(true);
      setPhase("setup");
    }
  }, [room.mode]);

  useEffect(() => {
    window.scrollTo({ top: 0, behavior: "instant" });
  }, [phase]);

  useEffect(() => {
    if (room.mode === "playing") return;
    window.scrollTo({ top: 0, behavior: "instant" });
    const frame = window.requestAnimationFrame(() => {
      const heading = document.querySelector<HTMLElement>(
        "[data-flow-heading], .game-shell > div:not([hidden]) .setup-wizard-header h1",
      );
      if (heading) {
        heading.tabIndex = -1;
        heading.focus({ preventScroll: true });
      }
    });
    return () => window.cancelAnimationFrame(frame);
  }, [room.mode]);

  useEffect(() => {
    const storedHistory = loadRoundHistory();
    roundHistoryRef.current = storedHistory;
    setRoundHistory(storedHistory);
  }, []);

  useEffect(() => {
    try {
      setIsTutorialPending(!hasSeenTutorial(window.localStorage));
    } catch {
      setIsTutorialPending(true);
    }
    setHasCheckedTutorial(true);
  }, []);

  useEffect(() => {
    if (!shouldAutoOpenTutorial(phase, hasCheckedTutorial, isTutorialPending)) return;
    setIsTutorialPending(false);
    setIsTutorialOpen(true);
  }, [hasCheckedTutorial, isTutorialPending, phase]);

  useEffect(
    () => () => {
      if (wrongFeedbackTimerRef.current !== null) {
        window.clearTimeout(wrongFeedbackTimerRef.current);
      }
    },
    [],
  );

  useEffect(() => {
    const desktop = window.matchMedia("(min-width: 64rem)");
    const handleResize = () => {
      setIsDrawerOpen(false);
      setOpenUtilityPanel(null);
    };
    desktop.addEventListener("change", handleResize);
    return () => desktop.removeEventListener("change", handleResize);
  }, []);

  useEffect(() => {
    if (!openUtilityPanel || isDrawerOpen) return;
    const dismiss = (event: PointerEvent) => {
      if (!desktopUtilityRef.current?.contains(event.target as Node)) setOpenUtilityPanel(null);
    };
    window.addEventListener("pointerdown", dismiss);
    return () => window.removeEventListener("pointerdown", dismiss);
  }, [openUtilityPanel, isDrawerOpen]);

  useEffect(() => {
    const drawer = drawerRef.current;
    if (!drawer) return;
    if (isDrawerOpen) drawer.showModal();
    else if (drawer.open) {
      drawer.close();
      drawerTriggerRef.current?.focus();
    }
  }, [isDrawerOpen]);

  useEffect(() => {
    setIsDrawerOpen(false);
    setOpenUtilityPanel(null);
  }, [phase, room.mode]);

  useEffect(() => {
    if (!openUtilityPanel) return;

    const handleEscape = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      setOpenUtilityPanel(null);
      utilityTriggerRef.current?.focus();
    };
    window.addEventListener("keydown", handleEscape);
    return () => window.removeEventListener("keydown", handleEscape);
  }, [openUtilityPanel]);

  useEffect(() => {
    const controller = new AbortController();
    async function loadFilters() {
      try {
        const response = await fetch("/api/filters", { signal: controller.signal });
        if (!response.ok) throw new Error("Could not load the catalog filters.");
        const data = (await response.json()) as FilterMetadata;
        setMetadata(data);
        const nextFilters: Filters = {
          genres: [],
          countries: [],
          artist: null,
          yearMin: data.year_min ?? 1960,
          yearMax: data.year_max ?? CURRENT_YEAR,
          popularityMin: data.popularity_min,
          popularityMax: data.popularity_max,
        };
        setDraftFilters(nextFilters);
        setActiveFilters(nextFilters);
      } catch (error) {
        if (!(error instanceof DOMException && error.name === "AbortError")) {
          setAppError("The API is not available yet. Start the backend and try again.");
        }
      }
    }
    void loadFilters();
    return () => controller.abort();
  }, []);

  useEffect(() => {
    const audio = audioRef.current;
    if (!audio) return;

    audio.pause();
    audio.currentTime = 0;
    audio.load();
    setPlaybackCursor(0);
    setIsAudioPlaying(false);
    setAudioError("");
  }, [previewUrl]);

  useEffect(() => {
    try {
      const saved = window.sessionStorage.getItem(VOLUME_SESSION_STORAGE_KEY);
      if (saved === null || saved.trim() === "") return;
      const value = Number(saved);
      if (Number.isFinite(value) && value >= 0 && value <= 1) setVolume(value);
    } catch {
      // Playback remains available when browser storage is disabled.
    }
  }, []);

  function changeVolume(value: number) {
    setVolume(value);
    try {
      window.sessionStorage.setItem(VOLUME_SESSION_STORAGE_KEY, String(value));
    } catch {
      // The volume still applies for the currently mounted game.
    }
  }

  useEffect(() => {
    if (audioRef.current) audioRef.current.volume = volume;
  }, [volume]);

  useEffect(() => {
    const audio = audioRef.current;
    if (!audio) return;

    const handleTimeUpdate = () => {
      setPlaybackCursor(
        phase === "playing" ? Math.min(audio.currentTime, unlockedDuration) : audio.currentTime,
      );
    };
    const handlePause = () => setIsAudioPlaying(false);
    const handlePlay = () => setIsAudioPlaying(true);
    const handlePlaying = () => setIsAudioPlaying(true);
    const handleEnded = () => setIsAudioPlaying(false);
    const handleError = () => {
      setIsAudioPlaying(false);
      setAudioError("This preview could not be played. Try the next clue or another song.");
    };
    const handleLoaded = () => setAudioError("");

    audio.addEventListener("timeupdate", handleTimeUpdate);
    audio.addEventListener("pause", handlePause);
    audio.addEventListener("play", handlePlay);
    audio.addEventListener("playing", handlePlaying);
    audio.addEventListener("ended", handleEnded);
    audio.addEventListener("error", handleError);
    audio.addEventListener("loadedmetadata", handleLoaded);
    return () => {
      audio.removeEventListener("timeupdate", handleTimeUpdate);
      audio.removeEventListener("pause", handlePause);
      audio.removeEventListener("play", handlePlay);
      audio.removeEventListener("playing", handlePlaying);
      audio.removeEventListener("ended", handleEnded);
      audio.removeEventListener("error", handleError);
      audio.removeEventListener("loadedmetadata", handleLoaded);
    };
  }, [phase, unlockedDuration]);

  useEffect(() => {
    if (!isRoomPlaying || !roomPlaybackRef.current) return;
    const playback = roomPlaybackRef.current;
    if (playback === "reveal" && phase !== "revealed") return;
    if (playback === "clue" && phase !== "playing") return;
    roomPlaybackRef.current = null;
    if (playback === "reveal") void playFullPreview(true);
    else if (audioRef.current?.paused) void playFullPreview();
  }, [isRoomPlaying, phase, attempt, previewUrl]);

  useEffect(() => {
    if (phase !== "playing" || !isAudioPlaying) return;
    let animationFrame = 0;
    const syncPlaybackCursor = () => {
      const audio = audioRef.current;
      if (!audio || audio.paused) return;

      const currentTime = Math.min(audio.currentTime, unlockedDuration);
      setPlaybackCursor(currentTime);
      if (audio.currentTime >= unlockedDuration - 0.01) {
        audio.pause();
        audio.currentTime = unlockedDuration;
        setPlaybackCursor(unlockedDuration);
        return;
      }
      animationFrame = window.requestAnimationFrame(syncPlaybackCursor);
    };
    syncPlaybackCursor();
    return () => window.cancelAnimationFrame(animationFrame);
  }, [isAudioPlaying, phase, unlockedDuration]);

  useEffect(() => {
    if (isRoomPlaying || currentSongId === null || phase !== "playing") return;
    let cancelled = false;
    void requestReveal(currentSongId)
      .then((song) => {
        if (!cancelled) setPreparedReveal({ id: currentSongId, song });
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, [currentSongId, phase]);

  useEffect(() => {
    if (phase === "setup") return;
    void import("../lib/sceneAssets").then((assets) => assets.preloadSceneModels()).catch(() => {});
    void import("./VinylRevealScene").catch(() => {});
    if (phase === "revealed" || phase === "loading") void prefetchCarousel().catch(() => {});
  }, [phase]);

  useEffect(() => {
    if (!room.state || (room.mode !== "playing" && room.mode !== "summary")) return;
    const current = room.state.current_round;
    if (!current) return;
    if (room.mode === "summary") setPreviewUrl(current.preview_url);
    const next = room.state.playlist.find((song) => song.position === current.position + 1);
    prefetchPreview(room.result ? current.preview_url : next?.reveal.preview_url);
    const currentSong = room.state.playlist.find((song) => song.position === current.position);
    void import("../lib/sceneAssets")
      .then((assets) =>
        Promise.all([
          assets.preloadSceneModels(),
          ...[currentSong, next].flatMap((song) =>
            song?.reveal.artwork_url ? [assets.preloadArtwork(song.reveal.artwork_url)] : [],
          ),
        ]),
      )
      .catch(() => {});
    void import("./VinylRevealScene").catch(() => {});
    void prefetchCarousel().catch(() => {});
  }, [room.mode, room.state?.code, room.state?.current_round?.position, room.result?.position]);

  useEffect(() => {
    if (
      !isRoomPlaying &&
      currentSongId !== null &&
      ((phase === "playing" && isAudioPlaying) || phase === "revealed")
    ) {
      void ensurePrefetchedRounds(activeFiltersRef.current);
    }
  }, [currentSongId, isAudioPlaying, phase]);

  useEffect(
    () => () => {
      ++playlistGenerationRef.current;
      prefetchControllerRef.current?.abort();
      prefetchedAudioRef.current?.removeAttribute("src");
      prefetchedAudioRef.current?.load();
    },
    [],
  );

  const loadSearchPage = useCallback(
    async (
      offset: number,
      normalizedQuery: string,
      generation: number,
    ): Promise<SearchResponse | null> => {
      const pageOffset = Math.max(0, Math.floor(offset / SEARCH_PAGE_SIZE) * SEARCH_PAGE_SIZE);
      if (
        generation !== searchGenerationRef.current ||
        loadedSearchPagesRef.current.has(pageOffset) ||
        pendingSearchPagesRef.current.has(pageOffset)
      )
        return null;

      pendingSearchPagesRef.current.add(pageOffset);
      setIsLoadingMoreResults(true);
      try {
        const payload = await requestSearchPage(pageOffset, normalizedQuery, browseSeedRef.current);
        if (generation !== searchGenerationRef.current) return null;

        loadedSearchPagesRef.current.add(pageOffset);
        searchTotalCountRef.current = payload.total;
        setSearchTotalCount(payload.total);
        const guessedIds = new Set(previousGuessesRef.current.map(({ id }) => id));
        const excludedFromPage = payload.items.flatMap((result, index) =>
          guessedIds.has(result.id) ? [payload.offset + index] : [],
        );
        if (excludedFromPage.length > 0) {
          setExcludedSearchIndexes((excluded) => [...new Set([...excluded, ...excludedFromPage])]);
        }
        setSearchResults((current) => {
          const indexedResults = new Map(
            current.map((result, index) => [result.searchIndex ?? index, result]),
          );
          payload.items.forEach((result, index) => {
            const searchIndex = payload.offset + index;
            if (!guessedIds.has(result.id)) {
              indexedResults.set(searchIndex, { ...result, searchIndex });
            }
          });
          return [...indexedResults.values()].sort(
            (left, right) => (left.searchIndex ?? 0) - (right.searchIndex ?? 0),
          );
        });
        return payload;
      } catch {
        return null;
      } finally {
        if (generation === searchGenerationRef.current) {
          pendingSearchPagesRef.current.delete(pageOffset);
          setIsLoadingMoreResults(pendingSearchPagesRef.current.size > 0);
        }
      }
    },
    [],
  );

  useEffect(() => {
    const generation = ++searchGenerationRef.current;
    loadedSearchPagesRef.current = new Set();
    pendingSearchPagesRef.current = new Set();
    searchTotalCountRef.current = 0;
    setIsLoadingMoreResults(false);
    const normalizedQuery = query.trim();
    if (phase !== "playing" || !shouldSearchSongs(normalizedQuery)) {
      setSearchResults([]);
      setSearchResultsQuery(normalizedQuery);
      setIsSearching(false);
      setIsLoadingMoreResults(false);
      setSearchTotalCount(0);
      setExcludedSearchIndexes([]);
      return;
    }

    if (!normalizedQuery) {
      // A new empty-search session gets a new order; its pages share this seed.
      const seed = Math.floor(Math.random() * 0x100000000);
      browseSeedRef.current = seed === browseSeedRef.current ? (seed + 1) >>> 0 : seed;
    }
    setIsSearching(true);
    let cancelled = false;
    const timeout = window.setTimeout(
      async () => {
        try {
          const payload = await requestSearchPage(0, normalizedQuery, browseSeedRef.current);
          if (generation !== searchGenerationRef.current) return;
          loadedSearchPagesRef.current.add(0);
          searchTotalCountRef.current = payload.total;
          setSearchResultsQuery(normalizedQuery);
          const excludedIndexes: number[] = [];
          setSearchResults(
            payload.items.flatMap((result, index) => {
              const searchIndex = payload.offset + index;
              if (previousGuessesRef.current.some((guess) => guess.id === result.id)) {
                excludedIndexes.push(searchIndex);
                return [];
              }
              return [{ ...result, searchIndex }];
            }),
          );
          setSearchTotalCount(payload.total);
          setExcludedSearchIndexes(excludedIndexes);
        } catch (error) {
          if (!cancelled && generation === searchGenerationRef.current) {
            setSearchResultsQuery(normalizedQuery);
            setSearchResults([]);
            setSearchTotalCount(0);
          }
        } finally {
          if (!cancelled && generation === searchGenerationRef.current) setIsSearching(false);
        }
      },
      normalizedQuery ? 350 : 0,
    );

    return () => {
      window.clearTimeout(timeout);
      cancelled = true;
      if (searchGenerationRef.current === generation) ++searchGenerationRef.current;
    };
  }, [phase, query]);

  const loadSearchResultAtIndex = useCallback(
    (index: number) => {
      const normalizedQuery = query.trim();
      if (phase !== "playing" || normalizedQuery !== searchResultsQuery) return;
      const total = searchTotalCountRef.current;
      if (total <= 0) return;
      const wrappedIndex = ((index % total) + total) % total;
      void loadSearchPage(wrappedIndex, normalizedQuery, searchGenerationRef.current);
    },
    [loadSearchPage, phase, query, searchResultsQuery],
  );

  const progressPercent = useMemo(() => {
    if (phase === "revealed") return 100;
    return Math.min(100, (playbackCursor / SNIPPET_DURATIONS[SNIPPET_DURATIONS.length - 1]) * 100);
  }, [phase, playbackCursor]);

  function stopAudio(reset = false) {
    const audio = audioRef.current;
    if (!audio) return;
    audio.pause();
    if (reset) {
      audio.currentTime = 0;
      setPlaybackCursor(0);
    }
  }

  function resetRoundUi() {
    setAppError("");
    setAudioError("");
    setRevealedSong(null);
    setOutcome(null);
    setAttempt(0);
    previousGuessesRef.current = [];
    setPreviousGuesses([]);
    setSelectedGuess(null);
    setQuery("");
    setSearchResults([]);
    setSearchResultsQuery("");
    setSearchTotalCount(0);
    setExcludedSearchIndexes([]);
  }

  function invalidatePrefetchedRounds() {
    ++playlistGenerationRef.current;
    prefetchControllerRef.current?.abort();
    prefetchControllerRef.current = null;
    prefetchedRoundsRef.current = [];
    prefetchPromiseRef.current = null;
    prefetchPreview();
  }

  function prefetchPreview(url?: string) {
    const audio = prefetchedAudioRef.current;
    if (!url) {
      audio?.removeAttribute("src");
      audio?.load();
      return;
    }
    if (audio?.getAttribute("src") === url) return;
    const nextAudio = audio ?? new Audio();
    nextAudio.preload = "auto";
    nextAudio.src = url;
    nextAudio.load();
    prefetchedAudioRef.current = nextAudio;
  }

  function ensurePrefetchedRounds(filters: Filters): Promise<void> {
    if (prefetchPromiseRef.current) return prefetchPromiseRef.current;

    const playlistGeneration = playlistGenerationRef.current;
    const controller = new AbortController();
    prefetchControllerRef.current = controller;
    let task: Promise<void>;
    task = fillRoundPrefetchQueue(
      prefetchedRoundsRef,
      excludedSongIdsRef,
      async (excludeIds) => {
        try {
          const response = await fetch("/api/round", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(createRoundRequest(filters, excludeIds)),
            signal: controller.signal,
          });
          if (playlistGeneration !== playlistGenerationRef.current || !response.ok) return null;
          const round = (await response.json()) as RoundResponse;
          return playlistGeneration === playlistGenerationRef.current ? round : null;
        } catch {
          return null;
        }
      },
      () => prefetchPreview(prefetchedRoundsRef.current[0]?.preview_url),
    ).finally(() => {
      if (prefetchPromiseRef.current === task) prefetchPromiseRef.current = null;
    });
    prefetchPromiseRef.current = task;
    return task;
  }

  function activateRound(round: RoundResponse, filters: Filters) {
    ++roundGenerationRef.current;
    stopAudio(true);
    resetRoundUi();
    activeFiltersRef.current = filters;
    setActiveFilters(filters);
    setCurrentSongId(round.song_id);
    setPreviewUrl(round.preview_url);
    setPhase("playing");
  }

  async function startRound(filters: Filters, idsToExclude = excludedSongIdsRef.current) {
    const generation = ++roundGenerationRef.current;
    stopAudio(true);
    setPhase("loading");
    resetRoundUi();

    const requestRound = async (excludeIds: number[]) => {
      return fetch("/api/round", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(createRoundRequest(filters, excludeIds)),
      });
    };

    try {
      let usedExclusions = idsToExclude;
      let response = await requestRound(usedExclusions);
      if (response.status === 404 && usedExclusions.length > 0) {
        invalidatePrefetchedRounds();
        usedExclusions = [];
        response = await requestRound([]);
      }
      if (!response.ok) {
        const message = await readApiError(response);
        throw new Error(message);
      }
      const round = (await response.json()) as RoundResponse;
      if (generation !== roundGenerationRef.current) return;

      activeFiltersRef.current = filters;
      setActiveFilters(filters);
      setCurrentSongId(round.song_id);
      setPreviewUrl(round.preview_url);
      const nextExclusions = [...usedExclusions, round.song_id];
      excludedSongIdsRef.current = nextExclusions;
      setPhase("playing");
    } catch (error) {
      if (generation !== roundGenerationRef.current) return;
      setCurrentSongId(null);
      setPreviewUrl("");
      setAppError(error instanceof Error ? error.message : "Could not begin a new round.");
      setPhase("setup");
    }
  }

  async function startNextRound() {
    if (isRoomPlaying) {
      room.next();
      return;
    }
    if (isAdvancingRoundRef.current) return;
    isAdvancingRoundRef.current = true;
    const playlistGeneration = playlistGenerationRef.current;
    try {
      if (prefetchedRoundsRef.current.length === 0 && prefetchPromiseRef.current) {
        await prefetchPromiseRef.current;
      }
      if (playlistGeneration !== playlistGenerationRef.current) return;

      const [nextRound, ...remainingRounds] = prefetchedRoundsRef.current;
      if (!nextRound) {
        await startRound(activeFiltersRef.current);
        return;
      }

      prefetchedRoundsRef.current = remainingRounds;
      activateRound(nextRound, activeFiltersRef.current);
    } finally {
      isAdvancingRoundRef.current = false;
    }
  }

  function beginGame(filters: SetupFilters = draftFilters) {
    if (filters.yearMin > filters.yearMax) {
      setAppError("The minimum year cannot be later than the maximum year.");
      return;
    }
    if (filters.popularityMin > filters.popularityMax) {
      setAppError("The minimum popularity cannot exceed the maximum popularity.");
      return;
    }
    setDraftFilters(filters);
    invalidatePrefetchedRounds();
    excludedSongIdsRef.current = [];
    void startRound(filters, []);
  }

  async function playSnippet() {
    const audio = audioRef.current;
    if (!audio) return;
    setAudioError("");
    if (audio.currentTime >= unlockedDuration - 0.05) {
      audio.currentTime = 0;
      setPlaybackCursor(0);
    }
    try {
      await audio.play();
    } catch {
      setAudioError("Playback was blocked. Tap Play again or check your browser audio settings.");
    }
  }

  function toggleSnippetPlayback() {
    if (isAudioPlaying) {
      audioRef.current?.pause();
    } else {
      void playSnippet();
    }
  }

  async function finishRound(nextOutcome: RoundOutcome) {
    if (isRoomPlaying) {
      room.action("give_up");
      return;
    }
    if (currentSongId === null) return;
    const generation = roundGenerationRef.current;
    let historyRecordId: string | null = null;
    if (recordedGenerationRef.current !== generation) {
      recordedGenerationRef.current = generation;
      historyRecordId = `${Date.now()}-${currentSongId}-${generation}`;
      persistRoundHistory({
        id: historyRecordId,
        songId: currentSongId,
        result: resultForRound(nextOutcome, attempt),
        completedAt: new Date().toISOString(),
      });
    }
    stopAudio(true);
    setRevealedSong(preparedReveal?.id === currentSongId ? preparedReveal.song : null);
    setOutcome(nextOutcome);
    setPhase("revealed");
    void playFullPreview(true);
    setIsRevealLoading(true);
    setAppError("");
    try {
      const song = await requestReveal(currentSongId);
      if (generation === roundGenerationRef.current) {
        setRevealedSong(song);
        if (historyRecordId) {
          const existing = roundHistoryRef.current.find((record) => record.id === historyRecordId);
          if (existing) {
            persistRoundHistory({
              ...existing,
              title: song.title,
              artist: song.artist,
              artworkUrl: song.artwork_url,
            });
          }
        }
      }
    } catch (error) {
      if (generation === roundGenerationRef.current) {
        setAppError(error instanceof Error ? error.message : "Could not reveal this song.");
      }
    } finally {
      if (generation === roundGenerationRef.current) setIsRevealLoading(false);
    }
  }

  async function retryReveal() {
    if (isRoomPlaying) {
      await room.resume();
      return;
    }
    if (currentSongId === null) return;
    const generation = roundGenerationRef.current;
    setIsRevealLoading(true);
    setAppError("");
    revealCache.delete(String(currentSongId));
    try {
      const song = await requestReveal(currentSongId);
      if (generation === roundGenerationRef.current) setRevealedSong(song);
    } catch (error) {
      if (generation === roundGenerationRef.current) {
        setAppError(error instanceof Error ? error.message : "Could not reveal this song.");
      }
    } finally {
      if (generation === roundGenerationRef.current) setIsRevealLoading(false);
    }
  }

  function persistRoundHistory(record: LocalRoundRecord) {
    const nextHistory = addOrUpdateRound(roundHistoryRef.current, record);
    roundHistoryRef.current = nextHistory;
    setRoundHistory(nextHistory);
    saveRoundHistory(nextHistory);
  }

  function advanceAttempt({ resumePlayback = false, preserveSearch = false } = {}) {
    if (isRoomPlaying) {
      roomPlaybackRef.current = resumePlayback ? "clue" : null;
      void room.action("skip");
      return;
    }
    if (!preserveSearch) {
      setSelectedGuess(null);
      setQuery("");
      setSearchResults([]);
      setSearchResultsQuery("");
      setSearchTotalCount(0);
      setExcludedSearchIndexes([]);
    }
    if (attempt === SNIPPET_DURATIONS.length - 1) {
      void finishRound("failed");
    } else {
      setAttempt((current) => current + 1);
      if (resumePlayback) {
        window.requestAnimationFrame(() => {
          const audio = audioRef.current;
          if (!audio || !audio.paused) return;
          setAudioError("");
          void audio.play().catch(() => {
            setAudioError(
              "Playback was blocked. Tap Play again or check your browser audio settings.",
            );
          });
        });
      }
    }
  }

  function submitGuess() {
    if (isRoomPlaying) {
      if (selectedGuess && query.trim() === searchResultsQuery && shouldSearchSongs(query))
        void room.action("guess", selectedGuess.id, selectedGuess);
      return;
    }
    if (
      !selectedGuess ||
      currentSongId === null ||
      query.trim() !== searchResultsQuery ||
      !shouldSearchSongs(query)
    )
      return;
    if (selectedGuess.id === currentSongId) {
      void finishRound("correct");
      return;
    }
    showWrongFeedback();
    if (!previousGuessesRef.current.some((guess) => guess.id === selectedGuess.id)) {
      const nextGuesses = [...previousGuessesRef.current, selectedGuess];
      previousGuessesRef.current = nextGuesses;
      setPreviousGuesses(nextGuesses);
    }
    const guessedIndex = selectedGuess.searchIndex;
    const { remainingResults, nextGuess } = excludeWrongGuess(searchResults, selectedGuess.id);
    setSearchResults(remainingResults);
    if (guessedIndex !== undefined) {
      setExcludedSearchIndexes((current) => [...current, guessedIndex]);
    }
    setSelectedGuess(nextGuess);
    advanceAttempt({ preserveSearch: true });
  }

  function showWrongFeedback() {
    if (wrongFeedbackTimerRef.current !== null) {
      window.clearTimeout(wrongFeedbackTimerRef.current);
    }
    setWrongFeedbackKey((current) => current + 1);
    wrongFeedbackTimerRef.current = window.setTimeout(() => {
      setWrongFeedbackKey(0);
      wrongFeedbackTimerRef.current = null;
    }, 900);
  }

  function selectSearchResult(result: SearchResult) {
    if (query.trim() !== searchResultsQuery || !shouldSearchSongs(query)) return;
    setSelectedGuess(result);
  }

  function clearGuessSearch() {
    setQuery("");
    setSelectedGuess(null);
    setSearchResults([]);
    setSearchResultsQuery("");
    setSearchTotalCount(0);
    setExcludedSearchIndexes([]);
  }

  async function playFullPreview(restart = false) {
    const audio = audioRef.current;
    if (!audio) return;
    setAudioError("");
    if (restart || audio.ended) audio.currentTime = 0;
    try {
      await audio.play();
    } catch {
      setAudioError("Playback was blocked. Tap the control again to continue.");
    }
  }

  function changeFilters() {
    if (isRoomPlaying) {
      room.exit();
      return;
    }
    ++roundGenerationRef.current;
    invalidatePrefetchedRounds();
    stopAudio(true);
    setDraftFilters(activeFilters);
    setPhase("setup");
    setAppError("");
    setOpenUtilityPanel(null);
  }

  const isRoundScreen =
    (phase === "playing" || phase === "revealed") &&
    (room.mode === "solo" || room.mode === "playing");

  const filterSummary = isRoomPlaying
    ? [
        room.summary?.filters.genres.join(" · ") || "All genres",
        room.summary?.filters.countries.map(countryDisplay).join(" · ") || "All origins",
      ].join(" — ")
    : [
        activeFilters.artist?.name ?? "All artists",
        activeFilters.genres.join(" · ") || "All genres",
        activeFilters.countries.map(countryDisplay).join(" · ") || "All origins",
      ].join(" — ");

  return (
    <section
      className={`game-shell${isRoundScreen ? " is-round" : " is-menu"}${isRoundScreen && phase === "playing" ? " is-playing" : ""}`}
      aria-live="polite"
    >
      {isRoundScreen && (
        <>
          <header className="desktop-game-header" ref={desktopUtilityRef}>
            <div className="brand-lockup has-utility-controls">
              <a className="brand" href="/" aria-label="Songuess home">
                Songuess<span aria-hidden="true">.</span>
              </a>
              <nav className="utility-controls" aria-label="Game options">
                <button
                  className="utility-icon-button"
                  type="button"
                  aria-label="Audio settings"
                  aria-expanded={openUtilityPanel === "volume"}
                  aria-controls="desktop-volume-panel"
                  onClick={(event) => {
                    utilityTriggerRef.current = event.currentTarget;
                    setOpenUtilityPanel((panel) => (panel === "volume" ? null : "volume"));
                  }}
                >
                  <svg viewBox="0 0 24 24" aria-hidden="true">
                    <path d="M4 7h10M18 7h2M4 17h2M10 17h10M14 4v6M6 14v6" />
                  </svg>
                </button>
                <button
                  className="utility-icon-button"
                  type="button"
                  aria-label="View personal statistics"
                  aria-expanded={openUtilityPanel === "stats"}
                  aria-controls="desktop-statistics-panel"
                  onClick={(event) => {
                    utilityTriggerRef.current = event.currentTarget;
                    setOpenUtilityPanel((panel) => (panel === "stats" ? null : "stats"));
                  }}
                >
                  <svg viewBox="0 0 24 24" aria-hidden="true">
                    <path d="M4 19V5M4 19h16M7 15l3-4 3 2 4-6" />
                    <path d="m15 7 2-.25.25 2" />
                  </svg>
                </button>
                <button
                  className="utility-icon-button"
                  type="button"
                  aria-label="How to play"
                  aria-haspopup="dialog"
                  onClick={(event) => {
                    utilityTriggerRef.current = event.currentTarget;
                    setOpenUtilityPanel(null);
                    setIsTutorialOpen(true);
                  }}
                >
                  <svg viewBox="0 0 24 24" aria-hidden="true">
                    <circle cx="12" cy="12" r="8" />
                    <path d="M12 11v5M12 8h.01" />
                  </svg>
                </button>
              </nav>
            </div>
            <p className="desktop-filter-summary">{filterSummary}</p>
            {isRoomPlaying && (
              <p className="desktop-room-progress">
                Song {room.result?.position ?? room.state?.current_round?.position} of{" "}
                {room.summary?.song_count ?? "?"} · {room.state?.total_points ?? 0} points
              </p>
            )}
            {!isDrawerOpen && openUtilityPanel === "volume" && (
              <div
                className="utility-popover"
                id="desktop-volume-panel"
                role="dialog"
                aria-label="Audio settings"
              >
                <span className="utility-popover-label">Volume</span>
                <VolumeControl
                  id="desktop-playback-volume"
                  volume={volume}
                  onChange={changeVolume}
                />
              </div>
            )}
            {!isDrawerOpen && openUtilityPanel === "stats" && (
              <div
                className="utility-popover desktop-statistics-panel"
                id="desktop-statistics-panel"
                role="dialog"
                aria-label="Personal statistics"
              >
                <PersonalStatistics history={roundHistory} stats={personalStats} />
              </div>
            )}
          </header>
          <button
            ref={drawerTriggerRef}
            className="utility-icon-button game-drawer-trigger"
            type="button"
            aria-label="Open game menu"
            aria-haspopup="dialog"
            aria-expanded={isDrawerOpen}
            aria-controls="game-drawer"
            onClick={() => setIsDrawerOpen(true)}
          >
            <svg viewBox="0 0 24 24" aria-hidden="true">
              <path d="M4 6h16M4 12h16M4 18h16" />
            </svg>
          </button>
          <dialog
            ref={drawerRef}
            id="game-drawer"
            className="game-drawer"
            aria-label="Game menu"
            onCancel={() => setIsDrawerOpen(false)}
            onClick={(event) => {
              if (event.target === event.currentTarget) setIsDrawerOpen(false);
            }}
          >
            <div className="game-drawer-content">
              <button
                className="utility-close-button drawer-close"
                type="button"
                aria-label="Close game menu"
                onClick={() => setIsDrawerOpen(false)}
              >
                ×
              </button>
              <a className="brand" href="/">
                Songuess<span aria-hidden="true">.</span>
              </a>
              <p className="drawer-filter-summary">{filterSummary}</p>
              {isRoomPlaying && (
                <p>
                  Song {room.result?.position ?? room.state?.current_round?.position} of{" "}
                  {room.summary?.song_count ?? "?"} · {room.state?.total_points ?? 0} points
                </p>
              )}
              <span className="eyebrow">Audio settings</span>
              <VolumeControl volume={volume} onChange={changeVolume} />
              <button
                className="secondary-action"
                type="button"
                aria-expanded={openUtilityPanel === "stats"}
                onClick={() => setOpenUtilityPanel((panel) => (panel === "stats" ? null : "stats"))}
              >
                Your statistics
              </button>
              {isDrawerOpen && openUtilityPanel === "stats" && (
                <PersonalStatistics history={roundHistory} stats={personalStats} />
              )}
              <button
                className="secondary-action"
                type="button"
                onClick={() => {
                  setIsDrawerOpen(false);
                  utilityTriggerRef.current = drawerTriggerRef.current;
                  setIsTutorialOpen(true);
                }}
              >
                How to play
              </button>
              <button className="secondary-action" type="button" onClick={changeFilters}>
                {isRoomPlaying ? "Back to room" : "Change mix"}
              </button>
            </div>
          </dialog>
        </>
      )}
      {!isRoundScreen && (
        <header className="brand-lockup">
          <a className="brand" href="/" aria-label="Songuess home">
            Songuess<span aria-hidden="true">.</span>
          </a>
        </header>
      )}

      {hasCheckedTutorial && isTutorialOpen && (phase === "playing" || phase === "revealed") && (
        <Tutorial
          onDismiss={() => {
            setIsTutorialOpen(false);
            window.requestAnimationFrame(() => utilityTriggerRef.current?.focus());
          }}
        />
      )}

      <audio ref={audioRef} src={previewUrl || undefined} preload="auto" />

      {wrongFeedbackKey > 0 && (
        <div
          className="wrong-feedback-overlay"
          key={wrongFeedbackKey}
          role="status"
          aria-live="assertive"
        >
          <span>Wrong</span>
        </div>
      )}

      {room.mode === "choose" ? (
        <ModeSelection onSolo={room.solo} onMultiplayer={room.multiplayer} />
      ) : room.mode === "multiplayer" ? (
        <MultiplayerMenu room={room} />
      ) : room.mode === "join" ? (
        <JoinChallenge room={room} />
      ) : room.mode === "summary" ? (
        <ChallengeRoom key={room.code} room={room} />
      ) : phase === "setup" || phase === "loading" ? (
        <>
          <div className="menu-setup-flow" hidden={room.mode === "details"}>
            <SetupNavigation
              label={room.mode === "solo" ? "Solo · Pick your mix" : "Multiplayer · Pick your mix"}
              onBack={room.mode === "solo" ? room.home : room.multiplayer}
              disabled={phase === "loading" || room.pending}
            />
            <SetupWizard
              filters={draftFilters}
              metadata={metadata}
              error={appError}
              loading={phase === "loading" || room.pending}
              startLabel={room.mode === "solo" ? "Play solo" : "Challenge details"}
              onChange={setDraftFilters}
              onStart={room.mode === "solo" ? beginGame : () => room.setMode("details")}
            />
          </div>
          {room.mode === "details" && <ChallengeDetails room={room} filters={draftFilters} />}
        </>
      ) : (
        <div className={`play-stack phase-${phase}`}>
          {isRoomPlaying && room.error && (
            <p role="alert" className="inline-error">
              {room.error}
            </p>
          )}
          {isRoomPlaying && room.uncertain && (
            <button type="button" disabled={room.pending} onClick={() => void room.resume()}>
              Refresh my progress to continue
            </button>
          )}

          <div className="play-mode-stage">
            <div className="gameplay-view">
              <div className="round-play-area">
                <section className="listening-card" aria-label="Audio controls">
                  {phase !== "revealed" && (
                    <div className="clue-status">
                      <PotentialPoints attempt={attempt} />
                    </div>
                  )}
                  <div className="vinyl-control-deck">
                    {phase === "revealed" && (
                      <div className="clue-status reveal-points-status">
                        <p
                          className="clue-readout outcome-readout"
                          aria-label={`${pointsForResult(resultForRound(outcome ?? "failed", attempt))} points`}
                        >
                          <span className="outcome-readout__points">
                            {pointsForResult(resultForRound(outcome ?? "failed", attempt))} points
                          </span>
                        </p>
                      </div>
                    )}
                    <VinylSleeveReveal
                      revealed={phase === "revealed"}
                      outcome={outcome}
                      song={revealedSong}
                      preloadArtworkUrl={
                        preparedReveal?.id === currentSongId
                          ? preparedReveal.song.artwork_url
                          : null
                      }
                      isPlaying={isAudioPlaying}
                      loading={isRevealLoading}
                      error={appError}
                      onRetry={() => void retryReveal()}
                    >
                      <VinylProgress
                        attempt={attempt}
                        progressPercent={progressPercent}
                        isPlaying={isAudioPlaying}
                        revealed={phase === "revealed"}
                        song={revealedSong}
                      />
                    </VinylSleeveReveal>
                    <nav className="game-actions-dock" aria-label="Round controls">
                      <button
                        disabled={isRoomPlaying && (room.pending || room.uncertain)}
                        className="dock-side-action dock-reveal"
                        type="button"
                        onClick={
                          phase === "revealed"
                            ? changeFilters
                            : () => advanceAttempt({ resumePlayback: true, preserveSearch: true })
                        }
                      >
                        {phase === "revealed"
                          ? isRoomPlaying
                            ? "Back to room"
                            : "Change filters"
                          : "Next clue"}
                      </button>
                      {phase !== "revealed" && (
                        <button
                          className={`dock-play-action dock-side-action${isAudioPlaying ? " is-playing" : ""}`}
                          type="button"
                          onClick={() => {
                            if (isAudioPlaying) audioRef.current?.pause();
                            else toggleSnippetPlayback();
                          }}
                          aria-label={isAudioPlaying ? "Pause clue" : "Play clue"}
                        >
                          {isAudioPlaying ? (
                            <svg viewBox="0 0 24 24" aria-hidden="true">
                              <path d="M7 5h4v14H7zM13 5h4v14h-4z" />
                            </svg>
                          ) : (
                            <svg viewBox="0 0 24 24" aria-hidden="true">
                              <path d="m8 5 11 7-11 7V5Z" />
                            </svg>
                          )}
                        </button>
                      )}
                      {phase === "revealed" && (
                        <button
                          disabled={isRoomPlaying && (room.pending || room.uncertain)}
                          className="dock-side-action dock-skip"
                          type="button"
                          onClick={() => void startNextRound()}
                        >
                          Next song
                        </button>
                      )}
                    </nav>
                  </div>
                  {audioError && <p className="inline-error">{audioError}</p>}
                </section>

                <div className="round-answer-surface">
                  <div className={`guess-collapse${phase === "revealed" ? " is-collapsed" : ""}`}>
                    <div>
                      <section className="guess-card" aria-label="Guess the song">
                        <div className="search-field">
                          <label className="sr-only" htmlFor="song-search">
                            Search by song title or artist
                          </label>
                          <input
                            id="song-search"
                            type="search"
                            value={query}
                            autoComplete="off"
                            placeholder="Song or artist…"
                            disabled={
                              phase === "revealed" ||
                              (isRoomPlaying && (room.pending || room.uncertain))
                            }
                            onChange={(event) => {
                              setQuery(event.target.value);
                              setSelectedGuess(null);
                              // Keep the settled deck in place during the debounce/request so the
                              // search surface never collapses. Its query identity is updated only
                              // when the latest response arrives, which prevents stale focus from
                              // surviving into the replacement ranking.
                              setIsSearching(true);
                            }}
                            aria-expanded={searchResults.length > 0}
                            aria-controls="search-results"
                          />
                          {query && phase === "playing" && (
                            <button
                              className="search-clear"
                              type="button"
                              aria-label="Clear song search"
                              onClick={clearGuessSearch}
                            >
                              <svg viewBox="0 0 24 24" aria-hidden="true">
                                <path d="m7 7 10 10M17 7 7 17" />
                              </svg>
                            </button>
                          )}
                          {phase === "playing" && !shouldSearchSongs(query) && (
                            <p role="status">Type at least 2 characters to search.</p>
                          )}
                          {phase === "playing" && shouldSearchSongs(query) && (
                            <AlbumGuessBrowser
                              results={searchResults}
                              query={searchResultsQuery}
                              isSearching={isSearching || isLoadingMoreResults}
                              totalCount={searchTotalCount}
                              excludedIndexes={excludedSearchIndexes}
                              onNeedIndex={loadSearchResultAtIndex}
                              onSelect={selectSearchResult}
                              onActiveChange={selectSearchResult}
                              selectedId={selectedGuess?.id}
                            />
                          )}
                        </div>
                        <button
                          className="primary-action"
                          type="button"
                          disabled={
                            (isRoomPlaying && (room.pending || room.uncertain)) ||
                            !selectedGuess ||
                            phase === "revealed" ||
                            query.trim() !== searchResultsQuery ||
                            !shouldSearchSongs(query)
                          }
                          onClick={submitGuess}
                        >
                          Guess
                        </button>
                      </section>
                    </div>
                  </div>
                </div>
              </div>
              {previousGuesses.length > 0 && (
                <section className="previous-guesses" aria-labelledby="previous-heading">
                  <span className="eyebrow" id="previous-heading">
                    Wrong
                  </span>
                  <ul>
                    {previousGuesses.map((guess) => (
                      <li key={guess.id}>
                        <span aria-hidden="true">×</span>
                        {guess.artwork_url ? (
                          <img src={guess.artwork_url} alt="" width="48" height="48" />
                        ) : (
                          <i aria-hidden="true">♪</i>
                        )}
                        <div>
                          <strong>{guess.title}</strong>
                          <small>{guess.artist}</small>
                          <small>
                            {guess.album ?? "Unknown album"} · {guess.release_year}
                          </small>
                        </div>
                      </li>
                    ))}
                  </ul>
                </section>
              )}
            </div>
          </div>
        </div>
      )}
    </section>
  );
}

const regionNames = new Intl.DisplayNames(["en"], { type: "region" });

export type CountryOption = { code: string; name: string; flag: string };

export function countryLabel(code: string): string {
  return regionNames.of(code) ?? code;
}

export function countryFlag(code: string): string {
  return [...code.toUpperCase()]
    .map((letter) => String.fromCodePoint(127397 + letter.charCodeAt(0)))
    .join("");
}

function countryDisplay(code: string): string {
  return `${countryFlag(code)} ${countryLabel(code)}`;
}

export function filterCountryOptions(countries: CountryOption[], query: string): CountryOption[] {
  const prefix = query.trim().toLocaleLowerCase();
  if (!prefix) return countries;
  return countries.filter(
    ({ code, name }) =>
      code.toLocaleLowerCase().startsWith(prefix) || name.toLocaleLowerCase().startsWith(prefix),
  );
}

type VinylProgressProps = {
  attempt: number;
  progressPercent: number;
  isPlaying: boolean;
  revealed: boolean;
  song: RevealedSong | null;
};

function VinylProgress({
  attempt,
  progressPercent,
  isPlaying,
  revealed,
  song,
}: VinylProgressProps) {
  const center = 120;
  const ringRadius = 106;
  const progressOffset = 100 - progressPercent;
  const unlockedPercent =
    (SNIPPET_DURATIONS[attempt] / SNIPPET_DURATIONS[SNIPPET_DURATIONS.length - 1]) * 100;
  const tickInnerRadius = 101;
  const tickOuterRadius = 111;
  const totalDuration = SNIPPET_DURATIONS[SNIPPET_DURATIONS.length - 1];

  return (
    <div
      className={`vinyl-progress${isPlaying ? " is-spinning" : ""}`}
      role="group"
      aria-label={
        revealed
          ? song
            ? `Answer: ${song.title} by ${song.artist}`
            : "Answer revealed"
          : `Clue ${attempt + 1} of ${SNIPPET_DURATIONS.length}, ${Math.round(progressPercent)} percent played`
      }
    >
      <div className="vinyl-flipper" aria-hidden="true">
        <div className="vinyl-face vinyl-front">
          <div className="vinyl-disc">
            <span />
          </div>
        </div>
      </div>
      {!revealed && (
        <svg className="vinyl-progress-ring" viewBox="0 0 240 240" aria-hidden="true">
          <circle className="vinyl-ring-locked" cx={center} cy={center} r={ringRadius} />
          <circle
            className="vinyl-ring-base"
            cx={center}
            cy={center}
            r={ringRadius}
            pathLength="100"
            strokeDasharray={`${unlockedPercent} 100`}
            transform={`rotate(-90 ${center} ${center})`}
          />
          <circle
            className="vinyl-ring-fill"
            cx={center}
            cy={center}
            r={ringRadius}
            pathLength="100"
            strokeDasharray="100"
            strokeDashoffset={progressOffset}
          />
          {[0, ...SNIPPET_DURATIONS.slice(0, -1)].map((duration) => {
            const angle = (duration / totalDuration) * Math.PI * 2 - Math.PI / 2;
            return (
              <line
                className="vinyl-ring-cut"
                key={duration}
                x1={center + Math.cos(angle) * tickInnerRadius}
                y1={center + Math.sin(angle) * tickInnerRadius}
                x2={center + Math.cos(angle) * tickOuterRadius}
                y2={center + Math.sin(angle) * tickOuterRadius}
              />
            );
          })}
        </svg>
      )}
    </div>
  );
}

type VolumeControlProps = {
  id?: string;
  volume: number;
  onChange: (volume: number) => void;
};

function VolumeControl({ id = "playback-volume", volume, onChange }: VolumeControlProps) {
  const percentage = Math.round(volume * 100);
  const trackStyle = { "--volume-level": `${percentage}%` } as CSSProperties;

  return (
    <div className="volume-control" style={trackStyle}>
      <svg className="volume-icon" viewBox="0 0 24 24" aria-hidden="true">
        <path d="M4 10v4h4l5 4V6l-5 4H4Z" />
        {volume > 0 && <path d="M16 9.5a4 4 0 0 1 0 5" />}
        {volume > 0.5 && <path d="M18.5 7a7.5 7.5 0 0 1 0 10" />}
      </svg>
      <label className="sr-only" htmlFor={id}>
        Playback volume
      </label>
      <input
        id={id}
        type="range"
        min="0"
        max="100"
        step="1"
        value={percentage}
        aria-valuetext={`${percentage} percent`}
        onChange={(event) => onChange(Number(event.target.value) / 100)}
      />
      <output htmlFor={id}>{percentage}%</output>
    </div>
  );
}

type PersonalStatisticsProps = {
  history: LocalRoundRecord[];
  stats: PersonalStats;
};

export function PersonalStatistics({ history, stats }: PersonalStatisticsProps) {
  const largestClueCount = Math.max(1, ...Object.values(stats.clueDistribution));
  return (
    <section className="personal-stats" aria-labelledby="personal-stats-heading">
      <div className="stats-heading">
        <div>
          <span className="eyebrow">This browser</span>
          <h2 id="personal-stats-heading">Your record</h2>
        </div>
        <strong>{stats.totalPoints} pts</strong>
      </div>

      {stats.totalSongs === 0 ? (
        <p className="muted">Your completed rounds will collect here.</p>
      ) : (
        <>
          <dl className="stats-summary">
            <div>
              <dt>Played</dt>
              <dd>{stats.totalSongs}</dd>
            </div>
            <div>
              <dt>Correct</dt>
              <dd>
                {stats.correctSongs} <small>{stats.correctPercentage}%</small>
              </dd>
            </div>
            <div>
              <dt>Missed</dt>
              <dd>
                {stats.notGuessedSongs} <small>{stats.notGuessedPercentage}%</small>
              </dd>
            </div>
            <div>
              <dt>Avg clue</dt>
              <dd>{stats.averageClue === null ? "—" : stats.averageClue.toFixed(1)}</dd>
            </div>
            <div>
              <dt>Streak</dt>
              <dd>{stats.currentStreak}</dd>
            </div>
            <div>
              <dt>Best</dt>
              <dd>{stats.bestStreak}</dd>
            </div>
          </dl>

          <div className="clue-distribution" aria-label="Correct answers by clue">
            {[1, 2, 3, 4, 5, 6].map((clue) => {
              const count = stats.clueDistribution[clue as 1 | 2 | 3 | 4 | 5 | 6];
              return (
                <div key={clue}>
                  <span>C{clue}</span>
                  <i
                    style={
                      { "--clue-height": `${(count / largestClueCount) * 100}%` } as CSSProperties
                    }
                  />
                  <b>{count}</b>
                </div>
              );
            })}
          </div>

          <details className="round-history" open>
            <summary>Recent rounds</summary>
            <ol>
              {history.slice(0, 12).map((record) => (
                <li key={record.id}>
                  <span>{record.artworkUrl ? <img src={record.artworkUrl} alt="" /> : "♪"}</span>
                  <div>
                    <strong>{record.title ?? `Song ${record.songId}`}</strong>
                    <small>
                      {record.artist ?? new Date(record.completedAt).toLocaleDateString()}
                    </small>
                  </div>
                  <b>
                    {record.result === "not_guessed"
                      ? "—"
                      : `${pointsForResult(record.result)} pts`}
                  </b>
                </li>
              ))}
            </ol>
          </details>
        </>
      )}
    </section>
  );
}

async function readApiError(response: Response): Promise<string> {
  try {
    const payload = (await response.json()) as { detail?: string | { message?: string } };
    if (typeof payload.detail === "string") return payload.detail;
    if (payload.detail?.message) return payload.detail.message;
  } catch {
    // The fallback below is intentionally user-safe for non-JSON responses.
  }
  return response.status === 404
    ? "No songs match these filters yet."
    : "Something went wrong. Try again.";
}

export function createRoundRequest(filters: SetupFilters, excludeIds: number[]) {
  return {
    genres: filters.genres,
    countries: filters.countries,
    artist_id: filters.artist?.id ?? null,
    year_min: filters.yearMin,
    year_max: filters.yearMax,
    popularity_min: filters.popularityMin,
    popularity_max: filters.popularityMax,
    exclude_ids: excludeIds,
  };
}
