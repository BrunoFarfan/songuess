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
import AlbumGuessBrowser from "./AlbumGuessBrowser";
import SetupWizard, { type ArtistOption, type SetupFilters } from "./SetupWizard";
import Tutorial, { hasSeenTutorial } from "./Tutorial";
import VinylSleeveReveal from "./VinylSleeveReveal";
import "./VinylControls.css";

const SNIPPET_DURATIONS = [1, 2, 4, 7, 11, 15] as const;
const CURRENT_YEAR = new Date().getFullYear();
const SEARCH_PAGE_SIZE = 40;

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

type RoundResponse = {
  song_id: number;
  preview_url: string;
};

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
  const [phase, setPhase] = useState<Phase>("setup");
  const [metadata, setMetadata] = useState<FilterMetadata | null>(null);
  const [draftFilters, setDraftFilters] = useState<Filters>(defaultFilters);
  const [activeFilters, setActiveFilters] = useState<Filters>(defaultFilters);
  const [currentSongId, setCurrentSongId] = useState<number | null>(null);
  const [previewUrl, setPreviewUrl] = useState("");
  const [attempt, setAttempt] = useState(0);
  const [playbackCursor, setPlaybackCursor] = useState(0);
  const [previousGuesses, setPreviousGuesses] = useState<SearchResult[]>([]);
  const [excludedSongIds, setExcludedSongIds] = useState<number[]>([]);
  const [outcome, setOutcome] = useState<RoundOutcome | null>(null);
  const [revealedSong, setRevealedSong] = useState<RevealedSong | null>(null);
  const [isRevealLoading, setIsRevealLoading] = useState(false);
  const [isRevealArmed, setIsRevealArmed] = useState(false);
  const [isAudioPlaying, setIsAudioPlaying] = useState(false);
  const [volume, setVolume] = useState(0.8);
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

  const audioRef = useRef<HTMLAudioElement>(null);
  const revealButtonRef = useRef<HTMLButtonElement>(null);
  const utilityControlsRef = useRef<HTMLElement>(null);
  const utilityTriggerRef = useRef<HTMLButtonElement | null>(null);
  const roundGenerationRef = useRef(0);
  const recordedGenerationRef = useRef<number | null>(null);
  const roundHistoryRef = useRef<LocalRoundRecord[]>([]);
  const previousGuessesRef = useRef<SearchResult[]>([]);
  const searchGenerationRef = useRef(0);
  const loadedSearchPagesRef = useRef(new Set<number>());
  const pendingSearchPagesRef = useRef(new Set<number>());
  const searchTotalCountRef = useRef(0);
  const wrongFeedbackTimerRef = useRef<number | null>(null);
  const unlockedDuration = SNIPPET_DURATIONS[attempt];
  const personalStats = useMemo(() => calculatePersonalStats(roundHistory), [roundHistory]);

  useEffect(() => {
    window.scrollTo({ top: 0, behavior: "instant" });
  }, [phase]);

  useEffect(() => {
    const storedHistory = loadRoundHistory();
    roundHistoryRef.current = storedHistory;
    setRoundHistory(storedHistory);
  }, []);

  useEffect(() => {
    try {
      setIsTutorialOpen(!hasSeenTutorial(window.localStorage));
    } catch {
      setIsTutorialOpen(true);
    }
    setHasCheckedTutorial(true);
  }, []);

  useEffect(
    () => () => {
      if (wrongFeedbackTimerRef.current !== null) {
        window.clearTimeout(wrongFeedbackTimerRef.current);
      }
    },
    [],
  );

  useEffect(() => {
    if (!isRevealArmed) return;
    const handleEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") setIsRevealArmed(false);
    };
    const handleOutsidePress = (event: PointerEvent) => {
      if (revealButtonRef.current?.contains(event.target as Node)) return;
      setIsRevealArmed(false);
    };
    window.addEventListener("keydown", handleEscape);
    window.addEventListener("pointerdown", handleOutsidePress);
    return () => {
      window.removeEventListener("keydown", handleEscape);
      window.removeEventListener("pointerdown", handleOutsidePress);
    };
  }, [isRevealArmed]);

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
    if (openUtilityPanel !== "volume") return;
    const handleOutsidePress = (event: PointerEvent) => {
      if (utilityControlsRef.current?.contains(event.target as Node)) return;
      closeUtilityPanel({ restoreFocus: false });
    };
    window.addEventListener("pointerdown", handleOutsidePress);
    return () => window.removeEventListener("pointerdown", handleOutsidePress);
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
      setAudioError("This preview could not be played. You can reveal it or try another song.");
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
        const params = new URLSearchParams({
          offset: String(pageOffset),
          limit: String(SEARCH_PAGE_SIZE),
        });
        if (normalizedQuery) params.set("q", normalizedQuery);
        const response = await fetch(`/api/songs/search?${params}`);
        if (!response.ok) throw new Error("Search failed");
        const payload = (await response.json()) as SearchResponse;
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
        pendingSearchPagesRef.current.delete(pageOffset);
        if (generation === searchGenerationRef.current) setIsLoadingMoreResults(false);
      }
    },
    [],
  );

  useEffect(() => {
    const generation = ++searchGenerationRef.current;
    loadedSearchPagesRef.current = new Set();
    pendingSearchPagesRef.current = new Set();
    searchTotalCountRef.current = 0;
    if (phase !== "playing") {
      setSearchResults([]);
      setSearchResultsQuery("");
      setIsSearching(false);
      setIsLoadingMoreResults(false);
      setSearchTotalCount(0);
      setExcludedSearchIndexes([]);
      return;
    }

    setIsSearching(true);
    const controller = new AbortController();
    const timeout = window.setTimeout(
      async () => {
        const normalizedQuery = query.trim();
        try {
          const params = new URLSearchParams({ offset: "0", limit: String(SEARCH_PAGE_SIZE) });
          if (normalizedQuery) params.set("q", normalizedQuery);
          const response = await fetch(`/api/songs/search?${params}`, {
            signal: controller.signal,
          });
          if (!response.ok) throw new Error("Search failed");
          const payload = (await response.json()) as SearchResponse;
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
          if (payload.total > SEARCH_PAGE_SIZE) {
            const tailOffset =
              Math.floor((payload.total - 1) / SEARCH_PAGE_SIZE) * SEARCH_PAGE_SIZE;
            void loadSearchPage(tailOffset, normalizedQuery, generation);
          }
        } catch (error) {
          if (!(error instanceof DOMException && error.name === "AbortError")) {
            setSearchResultsQuery(normalizedQuery);
            setSearchResults([]);
            setSearchTotalCount(0);
          }
        } finally {
          if (!controller.signal.aborted) setIsSearching(false);
        }
      },
      query.trim() ? 250 : 0,
    );

    return () => {
      window.clearTimeout(timeout);
      controller.abort();
    };
  }, [loadSearchPage, phase, query]);

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

  async function startRound(filters: Filters, idsToExclude = excludedSongIds) {
    const generation = ++roundGenerationRef.current;
    stopAudio(true);
    setPhase("loading");
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
    setIsRevealArmed(false);

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
        usedExclusions = [];
        response = await requestRound([]);
      }
      if (!response.ok) {
        const message = await readApiError(response);
        throw new Error(message);
      }
      const round = (await response.json()) as RoundResponse;
      if (generation !== roundGenerationRef.current) return;

      setActiveFilters(filters);
      setCurrentSongId(round.song_id);
      setPreviewUrl(round.preview_url);
      setExcludedSongIds([...usedExclusions, round.song_id]);
      setPhase("playing");
    } catch (error) {
      if (generation !== roundGenerationRef.current) return;
      setCurrentSongId(null);
      setPreviewUrl("");
      setAppError(error instanceof Error ? error.message : "Could not begin a new round.");
      setPhase("setup");
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
    setIsRevealArmed(false);
    stopAudio(true);
    setOutcome(nextOutcome);
    setPhase("revealed");
    void playFullPreview(true);
    setIsRevealLoading(true);
    setAppError("");
    try {
      const response = await fetch(`/api/songs/${currentSongId}`);
      if (!response.ok) throw new Error(await readApiError(response));
      const song = (await response.json()) as RevealedSong;
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
    if (currentSongId === null) return;
    const generation = roundGenerationRef.current;
    setIsRevealLoading(true);
    setAppError("");
    try {
      const response = await fetch(`/api/songs/${currentSongId}`);
      if (!response.ok) throw new Error(await readApiError(response));
      const song = (await response.json()) as RevealedSong;
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
    setIsRevealArmed(false);
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
    if (!selectedGuess || currentSongId === null) return;
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
    ++roundGenerationRef.current;
    stopAudio(true);
    setIsRevealArmed(false);
    setDraftFilters(activeFilters);
    setPhase("setup");
    setAppError("");
    setOpenUtilityPanel(null);
  }

  function toggleUtilityPanel(panel: "volume" | "stats", trigger: HTMLButtonElement) {
    utilityTriggerRef.current = trigger;
    setOpenUtilityPanel((current) => (current === panel ? null : panel));
  }

  function closeUtilityPanel({ restoreFocus = true } = {}) {
    setOpenUtilityPanel(null);
    if (restoreFocus) window.requestAnimationFrame(() => utilityTriggerRef.current?.focus());
  }

  return (
    <section className="game-shell" aria-live="polite">
      <header className="brand-lockup has-utility-controls">
        <a className="brand" href="/" aria-label="Songuess home">
          Songuess<span aria-hidden="true">.</span>
        </a>
        <nav ref={utilityControlsRef} className="utility-controls" aria-label="Game options">
          {(phase === "playing" || phase === "revealed") && (
            <>
              <div className="utility-control">
                <button
                  className="utility-icon-button"
                  type="button"
                  aria-label="Audio settings"
                  aria-expanded={openUtilityPanel === "volume"}
                  aria-controls="volume-panel"
                  onClick={(event) => toggleUtilityPanel("volume", event.currentTarget)}
                >
                  <svg viewBox="0 0 24 24" aria-hidden="true">
                    <path d="M4 7h10M18 7h2M4 17h2M10 17h10M14 4v6M6 14v6" />
                  </svg>
                </button>
                {openUtilityPanel === "volume" && (
                  <div
                    className="utility-popover volume-popover"
                    id="volume-panel"
                    role="dialog"
                    aria-label="Audio settings"
                  >
                    <span className="utility-popover-label">Volume</span>
                    <VolumeControl volume={volume} onChange={setVolume} />
                  </div>
                )}
              </div>
              <button
                className="utility-icon-button"
                type="button"
                aria-label="View personal statistics"
                aria-expanded={openUtilityPanel === "stats"}
                aria-controls="statistics-panel"
                onClick={(event) => toggleUtilityPanel("stats", event.currentTarget)}
              >
                <svg viewBox="0 0 24 24" aria-hidden="true">
                  <path d="M4 19V5M4 19h16M7 15l3-4 3 2 4-6" />
                  <path d="m15 7 2-.25.25 2" />
                </svg>
              </button>
            </>
          )}
          <button
            className="utility-icon-button tutorial-info-button"
            type="button"
            aria-label="How to play"
            aria-expanded={isTutorialOpen}
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
      </header>

      {hasCheckedTutorial && isTutorialOpen && (
        <Tutorial
          onDismiss={() => {
            setIsTutorialOpen(false);
            window.requestAnimationFrame(() => utilityTriggerRef.current?.focus());
          }}
        />
      )}

      <audio ref={audioRef} src={previewUrl || undefined} preload="metadata" />

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

      {phase === "setup" || phase === "loading" ? (
        <SetupWizard
          filters={draftFilters}
          metadata={metadata}
          error={appError}
          loading={phase === "loading"}
          onChange={setDraftFilters}
          onStart={beginGame}
        />
      ) : (
        <div className={`play-stack phase-${phase}`}>
          <div className="round-topline">
            <span>
              {[
                activeFilters.artist?.name ?? "All artists",
                activeFilters.genres.length ? activeFilters.genres.join(" · ") : "All genres",
                activeFilters.countries.length
                  ? activeFilters.countries.map(countryDisplay).join(" · ")
                  : "All origins",
              ].join(" — ")}
            </span>
          </div>

          <div
            className={`play-mode-stage${openUtilityPanel === "stats" ? " is-showing-stats" : ""}`}
          >
            {openUtilityPanel === "stats" ? (
              <section
                className="statistics-view"
                id="statistics-panel"
                aria-labelledby="personal-stats-heading"
              >
                <PersonalStatistics history={roundHistory} stats={personalStats} />
              </section>
            ) : (
              <div className="gameplay-view">
                <section className="listening-card" aria-label="Audio controls">
                  {phase !== "revealed" && (
                    <div className="clue-status">
                      <PotentialPoints attempt={attempt} />
                      <p className="clue-readout">
                        Current clue: {unlockedDuration}{" "}
                        {unlockedDuration === 1 ? "second" : "seconds"}
                      </p>
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
                        ref={revealButtonRef}
                        className={`dock-side-action dock-reveal${isRevealArmed ? " is-confirming" : ""}`}
                        type="button"
                        aria-label={
                          phase === "revealed"
                            ? "Change filters"
                            : isRevealArmed
                              ? "Confirm reveal song"
                              : "Reveal song"
                        }
                        aria-pressed={phase === "playing" ? isRevealArmed : undefined}
                        onClick={() => {
                          if (phase === "revealed") changeFilters();
                          else if (isRevealArmed) void finishRound("gave_up");
                          else setIsRevealArmed(true);
                        }}
                      >
                        {phase === "revealed" ? (
                          "Change filters"
                        ) : (
                          <span className="reveal-button-copy" aria-live="polite">
                            <span className="reveal-button-default">Reveal</span>
                            <span className="reveal-button-confirm">Reveal now?</span>
                          </span>
                        )}
                      </button>
                      {phase !== "revealed" && (
                        <button
                          className={`dock-play-action${isAudioPlaying ? " is-playing" : ""}`}
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
                      <button
                        className="dock-side-action dock-skip"
                        type="button"
                        onClick={
                          phase === "revealed"
                            ? () => void startRound(activeFilters)
                            : () => advanceAttempt({ resumePlayback: true, preserveSearch: true })
                        }
                      >
                        {phase === "revealed" ? "Next song" : "Next clue"}
                      </button>
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
                            disabled={phase === "revealed"}
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
                          {phase === "playing" && (
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
                          disabled={!selectedGuess || phase === "revealed"}
                          onClick={submitGuess}
                        >
                          Guess
                        </button>
                      </section>
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
            )}
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
          <circle className="vinyl-ring-base" cx={center} cy={center} r={ringRadius} />
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
  volume: number;
  onChange: (volume: number) => void;
};

function VolumeControl({ volume, onChange }: VolumeControlProps) {
  const percentage = Math.round(volume * 100);
  const trackStyle = { "--volume-level": `${percentage}%` } as CSSProperties;

  return (
    <div className="volume-control" style={trackStyle}>
      <svg className="volume-icon" viewBox="0 0 24 24" aria-hidden="true">
        <path d="M4 10v4h4l5 4V6l-5 4H4Z" />
        {volume > 0 && <path d="M16 9.5a4 4 0 0 1 0 5" />}
        {volume > 0.5 && <path d="M18.5 7a7.5 7.5 0 0 1 0 10" />}
      </svg>
      <label className="sr-only" htmlFor="playback-volume">
        Playback volume
      </label>
      <input
        id="playback-volume"
        type="range"
        min="0"
        max="100"
        step="1"
        value={percentage}
        aria-valuetext={`${percentage} percent`}
        onChange={(event) => onChange(Number(event.target.value) / 100)}
      />
      <output htmlFor="playback-volume">{percentage}%</output>
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
