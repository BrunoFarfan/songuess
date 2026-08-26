import {
  type CSSProperties,
  type RefObject,
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
} from "react";

import "./SetupWizard.css";

const CURRENT_YEAR = new Date().getFullYear();
const STEPS = ["artist", "genre", "country", "year", "popularity"] as const;

export type SetupWizardStep = (typeof STEPS)[number];

export interface ArtistOption {
  id: string;
  name: string;
  disambiguation: string | null;
  song_count: number;
}

export interface SetupFilters {
  genres: string[];
  countries: string[];
  artist: ArtistOption | null;
  yearMin: number;
  yearMax: number;
  popularityMin: number;
  popularityMax: number;
}

export interface SetupFilterMetadata {
  genres: string[];
  countries: string[];
  year_min: number | null;
  year_max: number | null;
  popularity_min: number;
  popularity_max: number;
  song_count: number;
}

export interface SetupWizardProps {
  filters: SetupFilters;
  metadata: SetupFilterMetadata | null;
  onChange: (filters: SetupFilters) => void;
  onStart: (filters: SetupFilters) => void;
  error?: string;
  loading?: boolean;
  artistSearchEndpoint?: string;
  contextualFiltersEndpoint?: string;
}

export interface SetupPreset {
  id: string;
  title: string;
  subtitle: string;
  genres?: string[];
  countries?: string[];
  year?: readonly [number, number];
  popularity?: readonly [number, number];
}

export const SETUP_PRESETS: readonly SetupPreset[] = [
  {
    id: "random",
    title: "Random",
    subtitle: "",
  },
  {
    id: "pop-this-century",
    title: "Pop This Century",
    subtitle: "Hooks from 2000 onward",
    genres: ["pop"],
    year: [2000, CURRENT_YEAR],
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
] as const;

type WizardView = "choose" | "custom" | "review";

export default function SetupWizard({
  filters,
  metadata,
  onChange,
  onStart,
  error = "",
  loading = false,
  artistSearchEndpoint = "/api/artists/search",
  contextualFiltersEndpoint = "/api/filters/context",
}: SetupWizardProps) {
  const [view, setView] = useState<WizardView>("choose");
  const [stepIndex, setStepIndex] = useState(0);
  const [selectedPreset, setSelectedPreset] = useState<string | null>(null);
  const [contextMetadata, setContextMetadata] = useState<SetupFilterMetadata | null>(null);
  const [resolvedContextKey, setResolvedContextKey] = useState<string | null>(null);
  const headingId = useId();

  const defaults = useMemo(() => defaultFilters(metadata), [metadata]);
  const available = contextMetadata ?? metadata;
  const availableDefaults = useMemo(
    () => ({
      ...defaults,
      yearMin: available?.year_min ?? defaults.yearMin,
      yearMax: available?.year_max ?? defaults.yearMax,
      popularityMin: defaults.popularityMin,
      popularityMax: defaults.popularityMax,
    }),
    [available, defaults],
  );
  const currentStep = STEPS[stepIndex];

  useEffect(() => {
    if (!metadata) return;
    const contextRequest = createFilterContextRequest(filters);
    const contextKey = JSON.stringify(contextRequest);
    setResolvedContextKey(null);
    const controller = new AbortController();
    const timeout = window.setTimeout(async () => {
      try {
        const response = await fetch(contextualFiltersEndpoint, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: contextKey,
          signal: controller.signal,
        });
        if (!response.ok) throw new Error("Contextual filters failed");
        const nextMetadata = (await response.json()) as SetupFilterMetadata;
        setContextMetadata(nextMetadata);
        setResolvedContextKey(contextKey);
        if (nextMetadata.song_count > 0) {
          const reconciled = reconcileContextualFilters(filters, nextMetadata, defaults);
          if (!filtersEqual(reconciled, filters)) onChange(reconciled);
        }
      } catch (contextError) {
        if (!(contextError instanceof DOMException && contextError.name === "AbortError")) {
          setContextMetadata(null);
        }
      }
    }, 100);
    return () => {
      window.clearTimeout(timeout);
      controller.abort();
    };
  }, [contextualFiltersEndpoint, defaults, filters, metadata, onChange]);

  const currentContextKey = JSON.stringify(createFilterContextRequest(filters));
  const contextIsCurrent = resolvedContextKey === currentContextKey;

  function choosePreset(preset: SetupPreset) {
    const nextFilters = resolvePreset(preset, metadata);
    onChange(nextFilters);
    setSelectedPreset(preset.id);
    setView("review");
  }

  function chooseCustom() {
    onChange(defaults);
    setSelectedPreset(null);
    setStepIndex(0);
    setView("custom");
  }

  function continueFromStep() {
    if (stepIndex === STEPS.length - 1) {
      setView("review");
      return;
    }
    setStepIndex((current) => current + 1);
  }

  function goBack() {
    if (stepIndex > 0) {
      setStepIndex((current) => current - 1);
    } else {
      setView("choose");
    }
  }

  function leaveReview() {
    if (selectedPreset) {
      setView("choose");
      return;
    }
    setStepIndex(STEPS.length - 1);
    setView("custom");
  }

  const presetTitle = SETUP_PRESETS.find(({ id }) => id === selectedPreset)?.title;
  const currentStepIsDefault = isStepDefault(filters, currentStep, availableDefaults);

  return (
    <section
      className={`setup-wizard view-${view}${view === "custom" ? ` step-${currentStep}` : ""}`}
      aria-labelledby={headingId}
      aria-busy={loading}
    >
      <header className="setup-wizard-header">
        <span className="setup-wizard-kicker">Select a pressing</span>
        <h1 id={headingId}>
          {view === "choose"
            ? "Pick your mix."
            : view === "review"
              ? (presetTitle ?? "Your custom mix")
              : stepTitle(currentStep)}
        </h1>
        {view === "custom" && (
          <p>
            {stepIndex + 1} / {STEPS.length}
          </p>
        )}
      </header>

      {metadata?.song_count === 0 && !error && (
        <p className="setup-wizard-notice">The catalog is empty.</p>
      )}
      {error && (
        <p className="setup-wizard-error" role="alert">
          {error}
        </p>
      )}

      {view === "choose" && (
        <div className="setup-wizard-chooser">
          <button className="setup-custom-choice" type="button" onClick={chooseCustom}>
            <span aria-hidden="true">00</span>
            <strong>Custom</strong>
            <b aria-hidden="true">↗</b>
          </button>
          <ol className="setup-preset-list">
            {SETUP_PRESETS.map((preset, index) => (
              <li key={preset.id}>
                <button type="button" onClick={() => choosePreset(preset)}>
                  <span>{String(index + 1).padStart(2, "0")}</span>
                  <strong>{preset.title}</strong>
                  <b aria-hidden="true">→</b>
                </button>
              </li>
            ))}
          </ol>
        </div>
      )}

      {view === "custom" && (
        <div className="setup-wizard-step" key={currentStep}>
          <nav className="setup-step-meter" aria-label="Setup progress">
            {STEPS.map((step, index) => (
              <span
                key={step}
                className={index === stepIndex ? "is-current" : index < stepIndex ? "is-done" : ""}
                aria-current={index === stepIndex ? "step" : undefined}
              />
            ))}
          </nav>

          {currentStep === "artist" && (
            <ArtistStep
              artist={filters.artist}
              endpoint={artistSearchEndpoint}
              onChange={(artist) =>
                onChange(clearDownstreamFilters({ ...filters, artist }, "artist", defaults))
              }
            />
          )}
          {currentStep === "genre" && (
            <OptionStep
              label="genres"
              options={available?.genres ?? []}
              selected={filters.genres}
              onChange={(genres) =>
                onChange(clearDownstreamFilters({ ...filters, genres }, "genre", defaults))
              }
            />
          )}
          {currentStep === "country" && (
            <OptionStep
              label="countries"
              options={available?.countries ?? []}
              selected={filters.countries}
              formatOption={countryDisplay}
              onChange={(countries) =>
                onChange(clearDownstreamFilters({ ...filters, countries }, "country", defaults))
              }
            />
          )}
          {currentStep === "year" && (
            <RangeStep
              label="Release year"
              min={available?.year_min ?? defaults.yearMin}
              max={available?.year_max ?? defaults.yearMax}
              low={filters.yearMin}
              high={filters.yearMax}
              onChange={(yearMin, yearMax) =>
                onChange(clearDownstreamFilters({ ...filters, yearMin, yearMax }, "year", defaults))
              }
            />
          )}
          {currentStep === "popularity" && (
            <RangeStep
              label="Popularity"
              min={defaults.popularityMin}
              max={defaults.popularityMax}
              low={filters.popularityMin}
              high={filters.popularityMax}
              onChange={(popularityMin, popularityMax) =>
                onChange({ ...filters, popularityMin, popularityMax })
              }
            />
          )}

          <div className="setup-step-actions">
            <button className="setup-back-action" type="button" aria-label="Back" onClick={goBack}>
              <span aria-hidden="true">←</span>
            </button>
            {!currentStepIsDefault && (
              <button
                className="setup-skip-action"
                type="button"
                aria-label={`Clear ${stepTitle(currentStep).toLocaleLowerCase()}`}
                onClick={() => onChange(resetStep(filters, currentStep, availableDefaults))}
              >
                <span aria-hidden="true">↺</span>
              </button>
            )}
            <button className="setup-continue-action" type="button" onClick={continueFromStep}>
              {currentStepIsDefault ? anyLabel(currentStep) : "Continue"} →
            </button>
          </div>
        </div>
      )}

      {view === "review" && (
        <div className="setup-wizard-review">
          <p className="setup-review-count" aria-live="polite">
            {contextIsCurrent && contextMetadata ? formatSongCount(contextMetadata.song_count) : ""}
          </p>
          <dl>
            <SummaryRow label="Artist" value={filters.artist?.name ?? "Any artist"} />
            <SummaryRow
              label="Genre"
              value={filters.genres.length ? filters.genres.join(" · ") : "Any genre"}
            />
            <SummaryRow
              label="Country"
              value={
                filters.countries.length
                  ? filters.countries.map(countryDisplay).join(" · ")
                  : "Any country"
              }
            />
            <SummaryRow
              label="Year"
              value={rangeSummary(
                filters.yearMin,
                filters.yearMax,
                availableDefaults.yearMin,
                availableDefaults.yearMax,
              )}
            />
            <SummaryRow
              label="Popularity"
              value={rangeSummary(
                filters.popularityMin,
                filters.popularityMax,
                availableDefaults.popularityMin,
                availableDefaults.popularityMax,
              )}
            />
          </dl>
          <div className="setup-review-actions">
            <button
              className="setup-back-action"
              type="button"
              aria-label="Back"
              onClick={leaveReview}
            >
              <span aria-hidden="true">←</span>
            </button>
            <button
              className="setup-start-action"
              type="button"
              disabled={loading || !contextIsCurrent || available?.song_count === 0}
              onClick={() => onStart(filters)}
            >
              <span>{loading ? "Cueing…" : "Play"}</span>
              <b aria-hidden="true">▶</b>
            </button>
          </div>
        </div>
      )}
    </section>
  );
}

interface ArtistStepProps {
  artist: ArtistOption | null;
  endpoint: string;
  onChange: (artist: ArtistOption | null) => void;
}

function ArtistStep({ artist, endpoint, onChange }: ArtistStepProps) {
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<ArtistOption[]>([]);
  const [searching, setSearching] = useState(false);
  const [overlayOpen, setOverlayOpen] = useState(false);
  const inputId = useId();
  const listId = useId();
  const inputRef = useRef<HTMLInputElement>(null);
  const searchStackRef = useRef<HTMLDivElement>(null);

  useDismissibleOverlay(searchStackRef, overlayOpen, setOverlayOpen);

  useEffect(() => {
    const normalizedQuery = query.trim();
    if (!normalizedQuery) {
      setResults([]);
      setSearching(false);
      return;
    }
    const controller = new AbortController();
    const timeout = window.setTimeout(async () => {
      setSearching(true);
      try {
        const separator = endpoint.includes("?") ? "&" : "?";
        const response = await fetch(
          `${endpoint}${separator}q=${encodeURIComponent(normalizedQuery)}`,
          { signal: controller.signal },
        );
        if (!response.ok) throw new Error("Artist search failed");
        const payload = (await response.json()) as ArtistOption[];
        setResults(payload.filter((option) => option.id !== artist?.id));
      } catch (searchError) {
        if (!(searchError instanceof DOMException && searchError.name === "AbortError")) {
          setResults([]);
        }
      } finally {
        if (!controller.signal.aborted) setSearching(false);
      }
    }, 200);
    return () => {
      window.clearTimeout(timeout);
      controller.abort();
    };
  }, [artist?.id, endpoint, query]);

  return (
    <div className="setup-artist-step">
      <label className="setup-visually-hidden" htmlFor={inputId}>
        Search artists
      </label>
      <div className="setup-search-stack" ref={searchStackRef}>
        {artist && (
          <div className="setup-inline-tokens" aria-label="Selected artist">
            <span className="setup-inline-token setup-artist-token">
              <span>
                <strong>{artist.name}</strong>
                {artist.disambiguation && <small>{artist.disambiguation}</small>}
              </span>
              <button
                type="button"
                aria-label={`Remove ${artist.name}`}
                onClick={() => onChange(null)}
              >
                ×
              </button>
            </span>
          </div>
        )}
        <div className="setup-search-anchor">
          <div className="setup-search-line">
            <span className="setup-search-icon" aria-hidden="true">
              ⌕
            </span>
            <div className="setup-inline-editor">
              <input
                ref={inputRef}
                id={inputId}
                type="search"
                value={query}
                placeholder={artist ? "Replace" : "Artist name"}
                autoComplete="off"
                aria-autocomplete="list"
                aria-expanded={shouldShowSetupOverlay(overlayOpen, query, results.length)}
                aria-controls={listId}
                onFocus={() => setOverlayOpen(Boolean(query.trim()))}
                onChange={(event) => {
                  setQuery(event.target.value);
                  setOverlayOpen(Boolean(event.target.value.trim()));
                }}
              />
            </div>
            <small aria-live="polite">{searching ? "Searching…" : ""}</small>
          </div>
          {shouldShowSetupOverlay(overlayOpen, query, results.length) && (
            <ul className="setup-search-results" id={listId} role="listbox" aria-label="Artists">
              {results.map((option) => (
                <li key={option.id}>
                  <button
                    type="button"
                    role="option"
                    aria-selected="false"
                    onClick={() => {
                      onChange(option);
                      setQuery("");
                      setResults([]);
                      setOverlayOpen(false);
                      window.requestAnimationFrame(() => inputRef.current?.focus());
                    }}
                  >
                    <span>
                      <strong>{option.name}</strong>
                      {option.disambiguation && <small>{option.disambiguation}</small>}
                    </span>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>
      </div>
    </div>
  );
}

interface OptionStepProps {
  label: string;
  options: string[];
  selected: string[];
  onChange: (selected: string[]) => void;
  formatOption?: (option: string) => string;
}

function OptionStep({
  label,
  options,
  selected,
  onChange,
  formatOption = (option) => option,
}: OptionStepProps) {
  const [query, setQuery] = useState("");
  const [overlayOpen, setOverlayOpen] = useState(false);
  const searchId = useId();
  const listId = useId();
  const inputRef = useRef<HTMLInputElement>(null);
  const searchStackRef = useRef<HTMLDivElement>(null);

  useDismissibleOverlay(searchStackRef, overlayOpen, setOverlayOpen);
  const visibleOptions = useMemo(() => {
    const prefix = query.trim().toLocaleLowerCase();
    if (!prefix) return options;
    return options.filter((option) => formatOption(option).toLocaleLowerCase().startsWith(prefix));
  }, [formatOption, options, query]);

  return (
    <fieldset className="setup-option-step">
      <legend className="setup-visually-hidden">Choose {label}</legend>
      <label className="setup-visually-hidden" htmlFor={searchId}>
        Find {label}
      </label>
      <div className="setup-search-stack" ref={searchStackRef}>
        {selected.length > 0 && (
          <div className="setup-inline-tokens" aria-label={`Selected ${label}`}>
            {selected.map((option) => (
              <span className="setup-inline-token" key={option}>
                <span>{formatOption(option)}</span>
                <button
                  type="button"
                  aria-label={`Remove ${formatOption(option)}`}
                  onClick={() => onChange(selected.filter((item) => item !== option))}
                >
                  ×
                </button>
              </span>
            ))}
          </div>
        )}
        <div className="setup-search-anchor">
          <div className="setup-search-line">
            <span className="setup-search-icon" aria-hidden="true">
              ⌕
            </span>
            <div className="setup-inline-editor">
              <input
                ref={inputRef}
                id={searchId}
                type="search"
                value={query}
                placeholder={`Find ${label}`}
                autoComplete="off"
                aria-expanded={overlayOpen}
                aria-controls={listId}
                onFocus={() => setOverlayOpen(true)}
                onChange={(event) => {
                  setQuery(event.target.value);
                  setOverlayOpen(true);
                }}
              />
            </div>
          </div>
          {overlayOpen && (
            <div className="setup-option-list" id={listId}>
              {visibleOptions.length === 0 ? (
                <p>{options.length === 0 ? "Loading catalogue…" : `No matching ${label}.`}</p>
              ) : (
                visibleOptions.map((option) => (
                  <label key={option}>
                    <input
                      type="checkbox"
                      checked={selected.includes(option)}
                      onChange={() => {
                        onChange(toggleSetupOption(selected, option));
                        setOverlayOpen(true);
                        window.requestAnimationFrame(() => inputRef.current?.focus());
                      }}
                    />
                    <span>{formatOption(option)}</span>
                    <b aria-hidden="true">{selected.includes(option) ? "●" : "○"}</b>
                  </label>
                ))
              )}
            </div>
          )}
        </div>
      </div>
    </fieldset>
  );
}

interface RangeStepProps {
  label: string;
  min: number;
  max: number;
  low: number;
  high: number;
  onChange: (low: number, high: number) => void;
}

function RangeStep({ label, min, max, low, high, onChange }: RangeStepProps) {
  const span = Math.max(1, max - min);
  const style = {
    "--setup-range-start": `${((low - min) / span) * 100}%`,
    "--setup-range-end": `${((high - min) / span) * 100}%`,
  } as CSSProperties;

  return (
    <div className="setup-range-step" style={style}>
      <div className="setup-range-readout">
        <span>{label}</span>
        <output>{low === min && high === max ? "Any" : `${low} — ${high}`}</output>
      </div>
      <div className="setup-dual-range">
        <div aria-hidden="true" />
        <input
          className="setup-range-low"
          type="range"
          min={min}
          max={max}
          value={low}
          aria-label={`Minimum ${label.toLocaleLowerCase()}`}
          onChange={(event) => onChange(Math.min(Number(event.target.value), high), high)}
        />
        <input
          className="setup-range-high"
          type="range"
          min={min}
          max={max}
          value={high}
          aria-label={`Maximum ${label.toLocaleLowerCase()}`}
          onChange={(event) => onChange(low, Math.max(Number(event.target.value), low))}
        />
      </div>
      <div className="setup-range-limits" aria-hidden="true">
        <span>{min}</span>
        <span>{max}</span>
      </div>
    </div>
  );
}

function SummaryRow({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <dt>{label}</dt>
      <dd>{value}</dd>
    </div>
  );
}

function useDismissibleOverlay<T extends HTMLElement>(
  containerRef: RefObject<T | null>,
  open: boolean,
  setOpen: (open: boolean) => void,
) {
  useEffect(() => {
    if (!open) return;

    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      event.preventDefault();
      setOpen(false);
    };
    const handlePointerDown = (event: PointerEvent) => {
      if (containerRef.current?.contains(event.target as Node)) return;
      setOpen(false);
    };

    window.addEventListener("keydown", handleKeyDown);
    window.addEventListener("pointerdown", handlePointerDown, true);
    return () => {
      window.removeEventListener("keydown", handleKeyDown);
      window.removeEventListener("pointerdown", handlePointerDown, true);
    };
  }, [containerRef, open, setOpen]);
}

export function shouldShowSetupOverlay(
  focused: boolean,
  query: string,
  resultCount: number,
): boolean {
  return focused && query.trim().length > 0 && resultCount > 0;
}

export function toggleSetupOption(selected: string[], option: string): string[] {
  return selected.includes(option)
    ? selected.filter((item) => item !== option)
    : [...selected, option];
}

export function formatSongCount(count: number): string {
  return `${count.toLocaleString()} ${count === 1 ? "song" : "songs"}`;
}

export function createFilterContextRequest(filters: SetupFilters) {
  return {
    genres: filters.genres,
    countries: filters.countries,
    artist_id: filters.artist?.id ?? null,
    year_min: filters.yearMin,
    year_max: filters.yearMax,
    popularity_min: filters.popularityMin,
    popularity_max: filters.popularityMax,
  };
}

export function clearDownstreamFilters(
  filters: SetupFilters,
  step: SetupWizardStep,
  defaults: SetupFilters,
): SetupFilters {
  if (step === "artist") {
    return {
      ...filters,
      genres: [],
      countries: [],
      yearMin: defaults.yearMin,
      yearMax: defaults.yearMax,
      popularityMin: defaults.popularityMin,
      popularityMax: defaults.popularityMax,
    };
  }
  if (step === "genre") {
    return {
      ...filters,
      countries: [],
      yearMin: defaults.yearMin,
      yearMax: defaults.yearMax,
      popularityMin: defaults.popularityMin,
      popularityMax: defaults.popularityMax,
    };
  }
  if (step === "country") {
    return {
      ...filters,
      yearMin: defaults.yearMin,
      yearMax: defaults.yearMax,
      popularityMin: defaults.popularityMin,
      popularityMax: defaults.popularityMax,
    };
  }
  if (step === "year") {
    return {
      ...filters,
      popularityMin: defaults.popularityMin,
      popularityMax: defaults.popularityMax,
    };
  }
  return filters;
}

export function reconcileContextualFilters(
  filters: SetupFilters,
  context: SetupFilterMetadata,
  defaults: SetupFilters,
): SetupFilters {
  const genreSet = new Set(context.genres);
  const countrySet = new Set(context.countries);
  const yearMin = context.year_min ?? defaults.yearMin;
  const yearMax = context.year_max ?? defaults.yearMax;
  return {
    ...filters,
    genres: filters.genres.filter((genre) => genreSet.has(genre)),
    countries: filters.countries.filter((country) => countrySet.has(country)),
    yearMin: clamp(filters.yearMin, yearMin, yearMax),
    yearMax: clamp(filters.yearMax, yearMin, yearMax),
    popularityMin: filters.popularityMin,
    popularityMax: filters.popularityMax,
  };
}

function filtersEqual(left: SetupFilters, right: SetupFilters): boolean {
  return (
    left.artist?.id === right.artist?.id &&
    left.genres.join("\u0000") === right.genres.join("\u0000") &&
    left.countries.join("\u0000") === right.countries.join("\u0000") &&
    left.yearMin === right.yearMin &&
    left.yearMax === right.yearMax &&
    left.popularityMin === right.popularityMin &&
    left.popularityMax === right.popularityMax
  );
}

export function defaultFilters(metadata: SetupFilterMetadata | null): SetupFilters {
  return {
    genres: [],
    countries: [],
    artist: null,
    yearMin: metadata?.year_min ?? 1960,
    yearMax: metadata?.year_max ?? CURRENT_YEAR,
    popularityMin: metadata?.popularity_min ?? 0,
    popularityMax: metadata?.popularity_max ?? 100,
  };
}

export function resolvePreset(
  preset: SetupPreset,
  metadata: SetupFilterMetadata | null,
): SetupFilters {
  const defaults = defaultFilters(metadata);
  const availableGenres = new Map(
    (metadata?.genres ?? []).map((genre) => [genre.toLocaleLowerCase(), genre]),
  );
  const availableCountries = new Set(metadata?.countries ?? []);
  const genres =
    metadata === null
      ? [...(preset.genres ?? [])]
      : (preset.genres ?? [])
          .map((genre) => availableGenres.get(genre.toLocaleLowerCase()))
          .filter((genre): genre is string => Boolean(genre));
  const countries = (preset.countries ?? []).filter(
    (country) => metadata === null || availableCountries.has(country),
  );

  return {
    ...defaults,
    genres,
    countries,
    yearMin: clamp(preset.year?.[0] ?? defaults.yearMin, defaults.yearMin, defaults.yearMax),
    yearMax: clamp(preset.year?.[1] ?? defaults.yearMax, defaults.yearMin, defaults.yearMax),
    popularityMin: preset.popularity?.[0] ?? defaults.popularityMin,
    popularityMax: preset.popularity?.[1] ?? defaults.popularityMax,
  };
}

export function resetStep(
  filters: SetupFilters,
  step: SetupWizardStep,
  defaults: SetupFilters,
): SetupFilters {
  if (step === "artist") return { ...filters, artist: null };
  if (step === "genre") return { ...filters, genres: [] };
  if (step === "country") return { ...filters, countries: [] };
  if (step === "year") {
    return { ...filters, yearMin: defaults.yearMin, yearMax: defaults.yearMax };
  }
  return {
    ...filters,
    popularityMin: defaults.popularityMin,
    popularityMax: defaults.popularityMax,
  };
}

function stepTitle(step: SetupWizardStep): string {
  return {
    artist: "Artist",
    genre: "Genre",
    country: "Country",
    year: "Year",
    popularity: "Popularity",
  }[step];
}

function anyLabel(step: SetupWizardStep): string {
  return {
    artist: "Any artist",
    genre: "Any genre",
    country: "Any country",
    year: "Any year",
    popularity: "Any popularity",
  }[step];
}

function isStepDefault(
  filters: SetupFilters,
  step: SetupWizardStep,
  defaults: SetupFilters,
): boolean {
  if (step === "artist") return filters.artist === null;
  if (step === "genre") return filters.genres.length === 0;
  if (step === "country") return filters.countries.length === 0;
  if (step === "year") {
    return filters.yearMin === defaults.yearMin && filters.yearMax === defaults.yearMax;
  }
  return (
    filters.popularityMin === defaults.popularityMin &&
    filters.popularityMax === defaults.popularityMax
  );
}

function rangeSummary(low: number, high: number, min: number, max: number): string {
  return low === min && high === max ? "Any" : `${low} — ${high}`;
}

function clamp(value: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, value));
}

const regionNames = new Intl.DisplayNames(["en"], { type: "region" });

function countryDisplay(code: string): string {
  const flag = [...code.toUpperCase()]
    .map((letter) => String.fromCodePoint(127397 + letter.charCodeAt(0)))
    .join("");
  return `${flag} ${regionNames.of(code) ?? code}`;
}
