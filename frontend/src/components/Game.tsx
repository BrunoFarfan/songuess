import { type CSSProperties, useEffect, useMemo, useRef, useState } from "react";

const SNIPPET_DURATIONS = [1, 2, 4, 7, 11, 15] as const;
const CURRENT_YEAR = new Date().getFullYear();

type Phase = "setup" | "loading" | "playing" | "revealed";
type RoundOutcome = "correct" | "failed" | "gave_up";

type Filters = {
  genres: string[];
  yearMin: number;
  yearMax: number;
  popularityMin: number;
  popularityMax: number;
};

type FilterMetadata = {
  genres: string[];
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
  artwork_url: string | null;
};

type RevealedSong = SearchResult & {
  album: string | null;
  release_year: number;
  artwork_url: string | null;
  genres: string[];
  preview_url: string;
};

const defaultFilters: Filters = {
  genres: [],
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
  const [isRevealConfirmOpen, setIsRevealConfirmOpen] = useState(false);
  const [isAudioPlaying, setIsAudioPlaying] = useState(false);
  const [volume, setVolume] = useState(0.8);
  const [audioError, setAudioError] = useState("");
  const [appError, setAppError] = useState("");
  const [query, setQuery] = useState("");
  const [searchResults, setSearchResults] = useState<SearchResult[]>([]);
  const [selectedGuess, setSelectedGuess] = useState<SearchResult | null>(null);
  const [isSearching, setIsSearching] = useState(false);

  const audioRef = useRef<HTMLAudioElement>(null);
  const roundGenerationRef = useRef(0);
  const unlockedDuration = SNIPPET_DURATIONS[attempt];

  useEffect(() => {
    window.scrollTo({ top: 0, behavior: "instant" });
  }, [phase]);

  useEffect(() => {
    if (!isRevealConfirmOpen) return;
    const handleEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") setIsRevealConfirmOpen(false);
    };
    window.addEventListener("keydown", handleEscape);
    return () => window.removeEventListener("keydown", handleEscape);
  }, [isRevealConfirmOpen]);

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
      if (phase === "playing" && audio.currentTime >= unlockedDuration - 0.025) {
        audio.pause();
        audio.currentTime = unlockedDuration;
        setPlaybackCursor(unlockedDuration);
        setIsAudioPlaying(false);
        return;
      }
      setPlaybackCursor(audio.currentTime);
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
    if (phase !== "playing") return;
    let animationFrame = 0;
    const syncPlaybackCursor = () => {
      const audio = audioRef.current;
      if (audio && !audio.paused) {
        setPlaybackCursor(Math.min(audio.currentTime, unlockedDuration));
      }
      animationFrame = window.requestAnimationFrame(syncPlaybackCursor);
    };
    animationFrame = window.requestAnimationFrame(syncPlaybackCursor);
    return () => window.cancelAnimationFrame(animationFrame);
  }, [phase, unlockedDuration]);

  useEffect(() => {
    if (phase !== "playing" || query.trim().length < 2 || selectedGuess) {
      setSearchResults([]);
      setIsSearching(false);
      return;
    }

    const controller = new AbortController();
    const timeout = window.setTimeout(async () => {
      setIsSearching(true);
      try {
        const response = await fetch(`/api/songs/search?q=${encodeURIComponent(query.trim())}`, {
          signal: controller.signal,
        });
        if (!response.ok) throw new Error("Search failed");
        const results = (await response.json()) as SearchResult[];
        setSearchResults(
          results.filter((result) => !previousGuesses.some((guess) => guess.id === result.id)),
        );
      } catch (error) {
        if (!(error instanceof DOMException && error.name === "AbortError")) {
          setSearchResults([]);
        }
      } finally {
        if (!controller.signal.aborted) setIsSearching(false);
      }
    }, 250);

    return () => {
      window.clearTimeout(timeout);
      controller.abort();
    };
  }, [phase, previousGuesses, query, selectedGuess]);

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
    setPreviousGuesses([]);
    setSelectedGuess(null);
    setQuery("");
    setSearchResults([]);
    setIsRevealConfirmOpen(false);

    const requestRound = async (excludeIds: number[]) => {
      return fetch("/api/round", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          genres: filters.genres,
          year_min: filters.yearMin,
          year_max: filters.yearMax,
          popularity_min: filters.popularityMin,
          popularity_max: filters.popularityMax,
          exclude_ids: excludeIds,
        }),
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

  function beginGame() {
    if (draftFilters.yearMin > draftFilters.yearMax) {
      setAppError("The minimum year cannot be later than the maximum year.");
      return;
    }
    if (draftFilters.popularityMin > draftFilters.popularityMax) {
      setAppError("The minimum popularity cannot exceed the maximum popularity.");
      return;
    }
    void startRound(draftFilters, []);
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

  function rewindSnippet() {
    const audio = audioRef.current;
    if (!audio) return;
    audio.pause();
    audio.currentTime = 0;
    setPlaybackCursor(0);
    setAudioError("");
  }

  async function finishRound(nextOutcome: RoundOutcome) {
    if (currentSongId === null) return;
    const generation = roundGenerationRef.current;
    setIsRevealConfirmOpen(false);
    stopAudio(true);
    setOutcome(nextOutcome);
    setPhase("revealed");
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

  function advanceAttempt() {
    const audio = audioRef.current;
    if (audio) {
      audio.pause();
      audio.currentTime = unlockedDuration;
    }
    setPlaybackCursor(unlockedDuration);
    setSelectedGuess(null);
    setQuery("");
    setSearchResults([]);
    if (attempt === SNIPPET_DURATIONS.length - 1) {
      void finishRound("failed");
    } else {
      setAttempt((current) => current + 1);
    }
  }

  function submitGuess() {
    if (!selectedGuess || currentSongId === null) return;
    if (selectedGuess.id === currentSongId) {
      void finishRound("correct");
      return;
    }
    if (!previousGuesses.some((guess) => guess.id === selectedGuess.id)) {
      setPreviousGuesses((guesses) => [...guesses, selectedGuess]);
    }
    advanceAttempt();
  }

  function selectSearchResult(result: SearchResult) {
    setSelectedGuess(result);
    setQuery(`${result.title} — ${result.artist}`);
    setSearchResults([]);
  }

  function clearGuessSearch() {
    setQuery("");
    setSelectedGuess(null);
    setSearchResults([]);
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

  function toggleGenre(genre: string) {
    setDraftFilters((filters) => ({
      ...filters,
      genres: filters.genres.includes(genre)
        ? filters.genres.filter((item) => item !== genre)
        : [...filters.genres, genre],
    }));
  }

  return (
    <section className="game-shell" aria-live="polite">
      <header className="brand-lockup">
        <a className="brand" href="/" aria-label="Songuess home">
          Songuess<span aria-hidden="true">.</span>
        </a>
      </header>

      <audio ref={audioRef} src={previewUrl || undefined} preload="metadata" />

      {phase === "setup" || phase === "loading" ? (
        <SetupScreen
          filters={draftFilters}
          metadata={metadata}
          error={appError}
          loading={phase === "loading"}
          onFiltersChange={setDraftFilters}
          onGenreToggle={toggleGenre}
          onPlay={beginGame}
        />
      ) : (
        <div className="play-stack">
          <div className="round-topline">
            <span>
              {activeFilters.genres.length ? activeFilters.genres.join(" · ") : "All genres"}
            </span>
          </div>

          {phase === "playing" ? (
            <>
              <section className="listening-card" aria-label="Audio snippet controls">
                <p className="clue-readout">
                  Current clue: {unlockedDuration} {unlockedDuration === 1 ? "second" : "seconds"}
                </p>
                <VinylProgress
                  attempt={attempt}
                  progressPercent={progressPercent}
                  isPlaying={isAudioPlaying}
                  onRewind={rewindSnippet}
                />
                <VolumeControl volume={volume} onChange={setVolume} />
                {audioError && <p className="inline-error">{audioError}</p>}
              </section>

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
                    onChange={(event) => {
                      setQuery(event.target.value);
                      setSelectedGuess(null);
                    }}
                    aria-expanded={searchResults.length > 0}
                    aria-controls="search-results"
                  />
                  {query && (
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
                  {isSearching && <span className="search-status">Searching…</span>}
                  {searchResults.length > 0 && (
                    <ul className="search-results" id="search-results" role="listbox">
                      {searchResults.map((result) => (
                        <li key={result.id}>
                          <button type="button" onClick={() => selectSearchResult(result)}>
                            {result.artwork_url ? (
                              <img src={result.artwork_url} alt="" />
                            ) : (
                              <span className="result-artwork-placeholder" aria-hidden="true">
                                ♪
                              </span>
                            )}
                            <span className="result-copy">
                              <strong>{result.title}</strong>
                              <small>{result.artist}</small>
                            </span>
                          </button>
                        </li>
                      ))}
                    </ul>
                  )}
                </div>
                {selectedGuess && (
                  <div className="selected-guess">
                    {selectedGuess.artwork_url ? (
                      <img
                        src={selectedGuess.artwork_url}
                        alt={`Cover artwork for ${selectedGuess.title}`}
                      />
                    ) : (
                      <span className="selected-artwork-placeholder" aria-hidden="true">
                        ♪
                      </span>
                    )}
                    <div>
                      <span>Selected</span>
                      <strong>{selectedGuess.title}</strong>
                      <small>{selectedGuess.artist}</small>
                    </div>
                  </div>
                )}
                <button
                  className="primary-action"
                  type="button"
                  disabled={!selectedGuess}
                  onClick={submitGuess}
                >
                  Guess
                </button>
              </section>

              {previousGuesses.length > 0 && (
                <section className="previous-guesses" aria-labelledby="previous-heading">
                  <span className="eyebrow" id="previous-heading">
                    Wrong
                  </span>
                  <ul>
                    {previousGuesses.map((guess) => (
                      <li key={guess.id}>
                        <span aria-hidden="true">×</span>
                        <div>
                          <strong>{guess.title}</strong>
                          <small>{guess.artist}</small>
                        </div>
                      </li>
                    ))}
                  </ul>
                </section>
              )}

              {searchResults.length === 0 && (
                <nav className="game-actions-dock" aria-label="Round controls">
                  <button
                    className="dock-side-action dock-reveal"
                    type="button"
                    onClick={() => setIsRevealConfirmOpen(true)}
                  >
                    Reveal
                  </button>
                  <button
                    className={`dock-play-action${isAudioPlaying ? " is-playing" : ""}`}
                    type="button"
                    onClick={toggleSnippetPlayback}
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
                  <button
                    className="dock-side-action dock-skip"
                    type="button"
                    onClick={advanceAttempt}
                  >
                    Next clue
                  </button>
                </nav>
              )}

              {isRevealConfirmOpen && (
                <div
                  className="confirm-backdrop"
                  role="presentation"
                  onMouseDown={(event) => {
                    if (event.currentTarget === event.target) setIsRevealConfirmOpen(false);
                  }}
                >
                  <section
                    className="confirm-dialog"
                    role="alertdialog"
                    aria-modal="true"
                    aria-labelledby="reveal-confirm-title"
                  >
                    <h2 id="reveal-confirm-title">Reveal song?</h2>
                    <div className="confirm-actions">
                      <button type="button" autoFocus onClick={() => setIsRevealConfirmOpen(false)}>
                        Cancel
                      </button>
                      <button type="button" onClick={() => void finishRound("gave_up")}>
                        Reveal
                      </button>
                    </div>
                  </section>
                </div>
              )}
            </>
          ) : (
            <RevealCard
              outcome={outcome}
              song={revealedSong}
              loading={isRevealLoading}
              error={appError}
              audioError={audioError}
              isPlaying={isAudioPlaying}
              onTogglePreview={() => {
                if (isAudioPlaying) audioRef.current?.pause();
                else void playFullPreview();
              }}
              onReplay={() => void playFullPreview(true)}
              onNext={() => void startRound(activeFilters)}
              onChangeFilters={() => {
                ++roundGenerationRef.current;
                stopAudio(true);
                setDraftFilters(activeFilters);
                setPhase("setup");
                setAppError("");
              }}
            />
          )}
        </div>
      )}
    </section>
  );
}

type SetupScreenProps = {
  filters: Filters;
  metadata: FilterMetadata | null;
  error: string;
  loading: boolean;
  onFiltersChange: (filters: Filters) => void;
  onGenreToggle: (genre: string) => void;
  onPlay: () => void;
};

function SetupScreen({
  filters,
  metadata,
  error,
  loading,
  onFiltersChange,
  onGenreToggle,
  onPlay,
}: SetupScreenProps) {
  return (
    <section className="setup-card" aria-labelledby="setup-heading">
      <div className="setup-intro">
        <h1 id="setup-heading">Pick your mix.</h1>
      </div>

      <div className="setup-controls">
        {metadata?.song_count === 0 && !error && (
          <div className="catalog-note">
            <strong>Catalog empty</strong>
          </div>
        )}
        {error && <p className="notice-error">{error}</p>}

        {(metadata === null || metadata.genres.length > 0) && (
          <fieldset>
            <legend>Genres</legend>
            <div className="genre-grid">
              {metadata === null ? (
                <span className="muted">Loading…</span>
              ) : (
                metadata.genres.map((genre) => (
                  <label className="genre-chip" key={genre}>
                    <input
                      type="checkbox"
                      checked={filters.genres.includes(genre)}
                      onChange={() => onGenreToggle(genre)}
                    />
                    <span>{genre}</span>
                  </label>
                ))
              )}
            </div>
          </fieldset>
        )}

        <div className="range-section">
          <div className="range-heading">
            <h2>Year</h2>
            <output>
              {filters.yearMin} — {filters.yearMax}
            </output>
          </div>
          <RangeSlider
            min={metadata?.year_min ?? 1960}
            max={metadata?.year_max ?? CURRENT_YEAR}
            low={filters.yearMin}
            high={filters.yearMax}
            lowLabel="Minimum release year"
            highLabel="Maximum release year"
            onChange={(yearMin, yearMax) => onFiltersChange({ ...filters, yearMin, yearMax })}
          />
        </div>

        <div className="range-section">
          <div className="range-heading">
            <h2>Popularity</h2>
            <output>
              {filters.popularityMin} — {filters.popularityMax}
            </output>
          </div>
          <RangeSlider
            min={0}
            max={100}
            low={filters.popularityMin}
            high={filters.popularityMax}
            lowLabel="Minimum popularity"
            highLabel="Maximum popularity"
            onChange={(popularityMin, popularityMax) =>
              onFiltersChange({ ...filters, popularityMin, popularityMax })
            }
          />
        </div>

        <button className="start-button" type="button" onClick={onPlay} disabled={loading}>
          <span>{loading ? "Loading…" : "Play"}</span>
          <span aria-hidden="true">↗</span>
        </button>
      </div>
    </section>
  );
}

type VinylProgressProps = {
  attempt: number;
  progressPercent: number;
  isPlaying: boolean;
  onRewind: () => void;
};

function VinylProgress({ attempt, progressPercent, isPlaying, onRewind }: VinylProgressProps) {
  const center = 120;
  const ringRadius = 106;
  const circumference = 2 * Math.PI * ringRadius;
  const progressOffset = circumference * (1 - progressPercent / 100);
  const tickInnerRadius = 101;
  const tickOuterRadius = 111;
  const totalDuration = SNIPPET_DURATIONS[SNIPPET_DURATIONS.length - 1];

  return (
    <div
      className={`vinyl-progress${isPlaying ? " is-spinning" : ""}`}
      role="group"
      aria-label={`Clue ${attempt + 1} of ${SNIPPET_DURATIONS.length}, ${Math.round(progressPercent)} percent played`}
    >
      <div className="vinyl-disc" aria-hidden="true">
        <span />
      </div>
      <svg className="vinyl-progress-ring" viewBox="0 0 240 240" aria-hidden="true">
        <circle className="vinyl-ring-base" cx={center} cy={center} r={ringRadius} />
        <circle
          className="vinyl-ring-fill"
          cx={center}
          cy={center}
          r={ringRadius}
          strokeDasharray={circumference}
          strokeDashoffset={progressOffset}
        />
        {SNIPPET_DURATIONS.slice(0, -1).map((duration) => {
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
      <button className="vinyl-rewind" type="button" onClick={onRewind} aria-label="Rewind clue">
        <ReplayIcon />
      </button>
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

function ReplayIcon() {
  return (
    <svg className="replay-icon" viewBox="0 0 24 24" aria-hidden="true">
      <path d="M8.3 7.5H4.5V3.7M4.8 7.1A8 8 0 1 1 4 13" />
    </svg>
  );
}

type RevealCardProps = {
  outcome: RoundOutcome | null;
  song: RevealedSong | null;
  loading: boolean;
  error: string;
  audioError: string;
  isPlaying: boolean;
  onTogglePreview: () => void;
  onReplay: () => void;
  onNext: () => void;
  onChangeFilters: () => void;
};

function RevealCard({
  outcome,
  song,
  loading,
  error,
  audioError,
  isPlaying,
  onTogglePreview,
  onReplay,
  onNext,
  onChangeFilters,
}: RevealCardProps) {
  const copy = outcome === "correct" ? "Correct" : outcome === "gave_up" ? "Revealed" : "Missed";

  return (
    <section className={`reveal-card outcome-${outcome ?? "loading"}`}>
      <div className="reveal-copy">
        <h1>{copy}</h1>
      </div>
      {loading && <div className="reveal-loading">Loading…</div>}
      {error && <p className="notice-error">{error}</p>}
      {song && (
        <>
          <div className="record-frame">
            {song.artwork_url ? (
              <img src={song.artwork_url} alt={`Cover artwork for ${song.title}`} />
            ) : (
              <div className="artwork-placeholder" aria-label="No cover artwork available">
                <span>♪</span>
              </div>
            )}
            <div className="song-details">
              <span>{song.release_year}</span>
              <h2>{song.title}</h2>
              <p>{song.artist}</p>
              {song.album && <small>{song.album}</small>}
              {song.genres.length > 0 && (
                <div className="genre-line">{song.genres.join(" · ")}</div>
              )}
            </div>
          </div>
          <div className="full-preview">
            <div>
              <h3>Full preview</h3>
            </div>
            <div className="full-preview-controls">
              <button className="primary-action" type="button" onClick={onTogglePreview}>
                {isPlaying ? "Pause preview" : "Play full preview"}
              </button>
              <button className="secondary-action replay-action" type="button" onClick={onReplay}>
                <ReplayIcon />
                <span>Replay</span>
              </button>
            </div>
            {audioError && <p className="inline-error">{audioError}</p>}
          </div>
        </>
      )}
      <div className="reveal-actions">
        <button className="start-button" type="button" onClick={onNext} disabled={!song}>
          <span>Next song</span>
          <span aria-hidden="true">→</span>
        </button>
        <button
          className="secondary-action change-filters-action"
          type="button"
          onClick={onChangeFilters}
        >
          Change filters
        </button>
      </div>
    </section>
  );
}

type RangeSliderProps = {
  min: number;
  max: number;
  low: number;
  high: number;
  lowLabel: string;
  highLabel: string;
  onChange: (low: number, high: number) => void;
};

function RangeSlider({ min, max, low, high, lowLabel, highLabel, onChange }: RangeSliderProps) {
  const span = Math.max(1, max - min);
  const trackStyle = {
    "--range-start": `${((low - min) / span) * 100}%`,
    "--range-end": `${((high - min) / span) * 100}%`,
  } as CSSProperties;

  return (
    <div className="dual-range" style={trackStyle}>
      <div className="dual-range-track" aria-hidden="true" />
      <input
        className="dual-range-input dual-range-low"
        type="range"
        min={min}
        max={max}
        value={low}
        aria-label={lowLabel}
        onChange={(event) => onChange(Math.min(Number(event.target.value), high), high)}
      />
      <input
        className="dual-range-input dual-range-high"
        type="range"
        min={min}
        max={max}
        value={high}
        aria-label={highLabel}
        onChange={(event) => onChange(low, Math.max(Number(event.target.value), low))}
      />
    </div>
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
