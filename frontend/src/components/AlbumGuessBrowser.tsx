import React, {
  type CSSProperties,
  type KeyboardEvent,
  type PointerEvent as ReactPointerEvent,
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
} from "react";

import PopularityScore from "./PopularityScore";
import "./AlbumGuessBrowser.css";

export type AlbumGuessOption = {
  id: number;
  title: string;
  artist: string;
  artwork_url: string | null;
  album: string | null;
  release_year: number;
  popularity_score: number | null;
};

export type AlbumGuessBrowserProps = {
  results: AlbumGuessOption[];
  onSelect: (result: AlbumGuessOption) => void;
  onActiveChange?: (result: AlbumGuessOption) => void;
  selectedId?: number | null;
  query?: string;
  isSearching?: boolean;
  className?: string;
  ariaLabel?: string;
  hasMore?: boolean;
  onNeedMore?: () => void | Promise<void>;
};

type AlbumItemStyle = CSSProperties & {
  "--album-order": number;
  "--album-depth": number;
  "--album-drag": string;
  "--album-drag-active": string;
  "--album-drag-rotation": string;
  "--album-shelf-angle": string;
  "--album-shelf-mobile-angle": string;
  "--album-shelf-scale": number;
  "--album-shelf-x": string;
  "--album-shelf-mobile-x": string;
  "--album-shelf-y": string;
  "--album-shelf-z": string;
};

const VISIBLE_ALBUM_COUNT = 11;
const SWIPE_MIN_DISTANCE = 18;
const SWIPE_MIN_VELOCITY = 0.18;
const SWIPE_STEP_DISTANCE = 64;
const SWIPE_PROJECTION_MS = 90;
const MAX_SWIPE_STEPS = 7;
const SWIPE_AXIS_LOCK_DISTANCE = 8;

export function swipeTraversalDelta({
  distance,
  elapsed,
}: {
  distance: number;
  elapsed: number;
}): number {
  const velocity = distance / Math.max(elapsed, 1);
  if (Math.abs(distance) < SWIPE_MIN_DISTANCE && Math.abs(velocity) < SWIPE_MIN_VELOCITY) {
    return 0;
  }

  const projectedMagnitude =
    Math.abs(distance) + Math.max(0, Math.abs(velocity) - 0.25) * SWIPE_PROJECTION_MS;
  const direction = distance < 0 ? 1 : -1;
  const steps = Math.min(
    MAX_SWIPE_STEPS,
    Math.max(1, Math.round(projectedMagnitude / SWIPE_STEP_DISTANCE)),
  );
  return direction * steps;
}

export function swipeLiveTraversalDelta(distance: number): number {
  return -Math.trunc(distance / SWIPE_STEP_DISTANCE);
}

type DragGesture = {
  pointerId: number;
  startX: number;
  startY: number;
  startTime: number;
  latestX: number;
  latestY: number;
  startIndex: number;
  appliedDelta: number;
  axis: "horizontal" | "vertical" | null;
};

type ResultsSnapshot = {
  resultSignature: string;
  selectedId: number | null;
  query: string;
};

type NavigationDirection = "backward" | "forward" | null;

export function shouldRequestMoreAlbums({
  activeIndex,
  resultCount,
  hasMore,
  navigationDirection,
}: {
  activeIndex: number;
  resultCount: number;
  hasMore: boolean;
  navigationDirection: NavigationDirection;
}): boolean {
  return (
    hasMore &&
    resultCount > 0 &&
    navigationDirection === "forward" &&
    activeIndex >= resultCount - 3
  );
}

function nextSurvivingResultIndex(
  previousIds: number[],
  removedIndex: number,
  results: AlbumGuessOption[],
) {
  const resultIndexes = new Map(results.map(({ id }, index) => [id, index]));

  for (let distance = 1; distance <= previousIds.length; distance += 1) {
    const nextId = previousIds[(removedIndex + distance) % previousIds.length];
    const nextIndex = resultIndexes.get(nextId);
    if (nextIndex !== undefined) return nextIndex;
  }

  return 0;
}

export function resolveAlbumActiveIndex({
  results,
  selectedId,
  retainedId,
  previousIds,
  activeIndex,
  queryChanged,
}: {
  results: AlbumGuessOption[];
  selectedId: number | null;
  retainedId: number | null;
  previousIds: number[];
  activeIndex: number;
  queryChanged: boolean;
}): number {
  const selectedIndex = results.findIndex(({ id }) => id === selectedId);
  if (queryChanged) return selectedIndex >= 0 ? selectedIndex : 0;

  const retainedIndex = results.findIndex(({ id }) => id === retainedId);
  const removedActiveIndex = retainedId === null ? -1 : previousIds.indexOf(retainedId);
  if (selectedIndex >= 0) return selectedIndex;
  if (retainedIndex >= 0) return retainedIndex;
  if (removedActiveIndex >= 0 && results.length > 0) {
    return nextSurvivingResultIndex(previousIds, removedActiveIndex, results);
  }
  return Math.min(activeIndex, Math.max(results.length - 1, 0));
}

/**
 * A controlled song-result picker that presents API results as a tactile album deck.
 * It accepts the existing `/api/songs/search` shape; album and year are optional so
 * richer search responses can be displayed later without changing the component API.
 */
export default function AlbumGuessBrowser({
  results,
  onSelect,
  onActiveChange,
  selectedId = null,
  query = "",
  isSearching = false,
  className = "",
  ariaLabel = "Song search results",
  hasMore = false,
  onNeedMore,
}: AlbumGuessBrowserProps) {
  const [activeIndex, setActiveIndex] = useState(0);
  const [dragOffset, setDragOffset] = useState(0);
  const [isDragging, setIsDragging] = useState(false);
  const optionIdPrefix = useId();
  const cardRefs = useRef(new Map<number, HTMLButtonElement>());
  const activeIdRef = useRef<number | null>(null);
  const previousResultIdsRef = useRef<number[]>([]);
  const resultsSnapshotRef = useRef<ResultsSnapshot>({
    resultSignature: "",
    selectedId: null,
    query: "",
  });
  const requestedLengthRef = useRef(-1);
  const navigationDirectionRef = useRef<NavigationDirection>(null);
  const dragGestureRef = useRef<DragGesture | null>(null);
  const suppressClickRef = useRef(false);
  const onActiveChangeRef = useRef(onActiveChange);
  const resultSignature = useMemo(() => results.map(({ id }) => id).join(":"), [results]);
  const normalizedQuery = query.trim();

  useEffect(() => {
    onActiveChangeRef.current = onActiveChange;
  }, [onActiveChange]);

  const resultsOrSelectionChanged =
    resultsSnapshotRef.current.resultSignature !== resultSignature ||
    resultsSnapshotRef.current.selectedId !== selectedId ||
    resultsSnapshotRef.current.query !== normalizedQuery;
  const queryChanged = resultsSnapshotRef.current.query !== normalizedQuery;

  let resolvedActiveIndex = Math.min(activeIndex, Math.max(results.length - 1, 0));
  if (resultsOrSelectionChanged) {
    const retainedId = activeIdRef.current;
    const previousIds = previousResultIdsRef.current;
    resolvedActiveIndex = resolveAlbumActiveIndex({
      results,
      selectedId,
      retainedId,
      previousIds,
      activeIndex,
      queryChanged,
    });
  }

  useEffect(() => {
    if (
      resultsSnapshotRef.current.resultSignature === resultSignature &&
      resultsSnapshotRef.current.selectedId === selectedId &&
      resultsSnapshotRef.current.query === normalizedQuery
    ) {
      return;
    }

    setActiveIndex(resolvedActiveIndex);
    previousResultIdsRef.current = results.map(({ id }) => id);
    resultsSnapshotRef.current = { resultSignature, selectedId, query: normalizedQuery };
    requestedLengthRef.current = -1;
  }, [normalizedQuery, resolvedActiveIndex, resultSignature, results, selectedId]);

  const boundedActiveIndex = resolvedActiveIndex;
  const activeResult = results[boundedActiveIndex];

  useEffect(() => {
    activeIdRef.current = activeResult?.id ?? null;
    if (activeResult) onActiveChangeRef.current?.(activeResult);
  }, [activeResult]);

  useEffect(() => {
    if (
      !onNeedMore ||
      !shouldRequestMoreAlbums({
        activeIndex: boundedActiveIndex,
        resultCount: results.length,
        hasMore,
        navigationDirection: navigationDirectionRef.current,
      }) ||
      requestedLengthRef.current === results.length
    ) {
      return;
    }

    requestedLengthRef.current = results.length;
    void onNeedMore();
  }, [boundedActiveIndex, hasMore, onNeedMore, results.length]);

  const visibleResults = useMemo(() => {
    if (results.length === 0) return [];
    const visibleCount = Math.min(VISIBLE_ALBUM_COUNT, results.length);
    const albumsBeforeActive = Math.floor((visibleCount - 1) / 2);

    return Array.from({ length: visibleCount }, (_, position) => {
      const offset = position - albumsBeforeActive;
      const index = (boundedActiveIndex + offset + results.length) % results.length;
      return { result: results[index], index, offset, position };
    });
  }, [boundedActiveIndex, resultSignature, results]);

  function moveTo(index: number, focus = false, navigationDirection: NavigationDirection = null) {
    if (results.length === 0) return;
    navigationDirectionRef.current = navigationDirection;
    const nextIndex = (index + results.length) % results.length;
    setActiveIndex(nextIndex);
    if (focus) {
      window.requestAnimationFrame(() => cardRefs.current.get(results[nextIndex].id)?.focus());
    }
  }

  function handleKeyboard(event: KeyboardEvent<HTMLDivElement>) {
    if (event.key === "ArrowLeft") {
      event.preventDefault();
      moveTo(boundedActiveIndex - 1, true, "backward");
    } else if (event.key === "ArrowRight") {
      event.preventDefault();
      moveTo(boundedActiveIndex + 1, true, "forward");
    } else if (event.key === "Home") {
      event.preventDefault();
      moveTo(0, true, "backward");
    } else if (event.key === "End") {
      event.preventDefault();
      moveTo(results.length - 1, true, "forward");
    }
  }

  useEffect(() => {
    function handleWindowNavigation(event: globalThis.KeyboardEvent) {
      if (event.defaultPrevented || (event.key !== "ArrowLeft" && event.key !== "ArrowRight")) {
        return;
      }

      const target = event.target;
      const isTypingTarget =
        target instanceof HTMLElement &&
        (target.matches("input, textarea, select") ||
          target.isContentEditable ||
          Boolean(target.closest("[contenteditable='true']")));
      if (isTypingTarget) return;

      event.preventDefault();
      moveTo(
        boundedActiveIndex + (event.key === "ArrowLeft" ? -1 : 1),
        false,
        event.key === "ArrowLeft" ? "backward" : "forward",
      );
    }

    window.addEventListener("keydown", handleWindowNavigation);
    return () => window.removeEventListener("keydown", handleWindowNavigation);
  }, [boundedActiveIndex, resultSignature, results]);

  function beginSwipe(event: ReactPointerEvent<HTMLDivElement>) {
    if (event.pointerType === "mouse") return;
    dragGestureRef.current = {
      pointerId: event.pointerId,
      startX: event.clientX,
      startY: event.clientY,
      startTime: event.timeStamp,
      latestX: event.clientX,
      latestY: event.clientY,
      startIndex: boundedActiveIndex,
      appliedDelta: 0,
      axis: null,
    };
    setIsDragging(true);
    try {
      event.currentTarget.setPointerCapture(event.pointerId);
    } catch {
      // Synthetic touch events and a few older WebViews do not expose a
      // capturable native pointer. The stage handlers still track the gesture.
    }
  }

  function updateSwipe(event: ReactPointerEvent<HTMLDivElement>) {
    const gesture = dragGestureRef.current;
    if (!gesture || gesture.pointerId !== event.pointerId) return;
    const coalescedEvents = event.nativeEvent.getCoalescedEvents?.() ?? [];
    const latestEvent = coalescedEvents.at(-1) ?? event.nativeEvent;
    gesture.latestX = latestEvent.clientX;
    gesture.latestY = latestEvent.clientY;
    const distance = latestEvent.clientX - gesture.startX;
    const verticalDistance = latestEvent.clientY - gesture.startY;

    if (
      gesture.axis === null &&
      Math.hypot(distance, verticalDistance) >= SWIPE_AXIS_LOCK_DISTANCE
    ) {
      gesture.axis = Math.abs(distance) > Math.abs(verticalDistance) ? "horizontal" : "vertical";
    }
    if (gesture.axis !== "horizontal") return;

    const liveDelta = swipeLiveTraversalDelta(distance);
    if (liveDelta !== gesture.appliedDelta) {
      gesture.appliedDelta = liveDelta;
      moveTo(gesture.startIndex + liveDelta, false, liveDelta < 0 ? "backward" : "forward");
    }
    const consumedDistance = -liveDelta * SWIPE_STEP_DISTANCE;
    const remainingDistance = distance - consumedDistance;
    setDragOffset(Math.max(-SWIPE_STEP_DISTANCE, Math.min(SWIPE_STEP_DISTANCE, remainingDistance)));

    if (Math.abs(distance) >= SWIPE_MIN_DISTANCE) suppressClickRef.current = true;
  }

  function finishSwipe(event: ReactPointerEvent<HTMLDivElement>, cancelled = false) {
    const gesture = dragGestureRef.current;
    if (!gesture || gesture.pointerId !== event.pointerId) return;
    const finalX = cancelled ? gesture.latestX : event.clientX;
    const finalY = cancelled ? gesture.latestY : event.clientY;
    const distance = finalX - gesture.startX;
    const verticalDistance = finalY - gesture.startY;
    const elapsed = Math.max(event.timeStamp - gesture.startTime, 1);
    dragGestureRef.current = null;
    setDragOffset(0);
    setIsDragging(false);

    if (event.currentTarget.hasPointerCapture?.(event.pointerId)) {
      event.currentTarget.releasePointerCapture(event.pointerId);
    }

    const isHorizontalGesture =
      gesture.axis === "horizontal" ||
      (gesture.axis === null && Math.abs(distance) > Math.abs(verticalDistance));
    const traversalDelta = cancelled
      ? gesture.appliedDelta
      : isHorizontalGesture
        ? swipeTraversalDelta({ distance, elapsed })
        : 0;
    if (traversalDelta !== gesture.appliedDelta) {
      moveTo(
        gesture.startIndex + traversalDelta,
        false,
        traversalDelta < 0 ? "backward" : "forward",
      );
    }
    if (traversalDelta !== 0 || gesture.appliedDelta !== 0) {
      suppressClickRef.current = true;
      window.setTimeout(() => {
        suppressClickRef.current = false;
      }, 0);
    }
  }

  if (isSearching && results.length === 0) {
    return (
      <div className={`album-guess-browser is-loading ${className}`.trim()} role="status">
        <span className="album-guess-loading-disc" aria-hidden="true" />
        <span>Pulling records…</span>
      </div>
    );
  }

  if (results.length === 0) {
    return query.trim().length >= 2 ? (
      <p className={`album-guess-empty ${className}`.trim()} role="status">
        No records found for “{query.trim()}”.
      </p>
    ) : null;
  }

  return (
    <section
      className={`album-guess-browser${isDragging ? " is-dragging" : ""} ${className}`.trim()}
      aria-label={ariaLabel}
      onKeyDown={handleKeyboard}
    >
      {isSearching && (
        <div className="album-guess-loading-overlay" role="status">
          <span className="album-guess-loading-disc" aria-hidden="true" />
          <span>Pulling records…</span>
        </div>
      )}
      <div className="album-guess-deck">
        <button
          className="album-guess-arrow is-previous"
          type="button"
          aria-label="Previous song"
          onClick={() => moveTo(boundedActiveIndex - 1, true, "backward")}
        >
          <svg viewBox="0 0 24 24" aria-hidden="true">
            <path d="m15 5-7 7 7 7" />
          </svg>
        </button>

        <div
          className="album-guess-stage"
          role="listbox"
          aria-label={ariaLabel}
          aria-activedescendant={`${optionIdPrefix}-${activeResult.id}`}
          style={{ "--album-drag": `${dragOffset}px` } as CSSProperties}
          onPointerDown={beginSwipe}
          onPointerMove={updateSwipe}
          onPointerUp={finishSwipe}
          onPointerCancel={(event) => finishSwipe(event, true)}
        >
          {visibleResults.map(({ result, index, offset, position }) => {
            const depth = Math.abs(offset);
            const side = Math.sign(offset);
            const shelfX = side * (4.8 + Math.max(depth - 1, 0) * 2.1);
            const shelfMobileX = side * (3.7 + Math.max(depth - 1, 0) * 0.86);
            const style: AlbumItemStyle = {
              "--album-order": position,
              "--album-depth": depth,
              "--album-drag": `${dragOffset}px`,
              "--album-drag-active": `${dragOffset * 0.34}px`,
              "--album-drag-rotation": `${dragOffset / -5}deg`,
              "--album-shelf-angle": `${side < 0 ? -68 : 68}deg`,
              "--album-shelf-mobile-angle": `${side < 0 ? -76 : 76}deg`,
              "--album-shelf-scale": 0.86,
              "--album-shelf-x": `${shelfX}rem`,
              "--album-shelf-mobile-x": `${shelfMobileX}rem`,
              "--album-shelf-y": "0.45rem",
              "--album-shelf-z": "0px",
              zIndex: VISIBLE_ALBUM_COUNT * 2 - depth * 2 - (side > 0 ? 1 : 0),
            };

            return (
              <div
                className={`album-guess-item${index === boundedActiveIndex ? " is-active" : ""}`}
                key={result.id}
                role="presentation"
                style={style}
              >
                <div className="album-guess-deal" role="presentation">
                  <button
                    ref={(node) => {
                      if (node) cardRefs.current.set(result.id, node);
                      else cardRefs.current.delete(result.id);
                    }}
                    className="album-guess-cover"
                    id={`${optionIdPrefix}-${result.id}`}
                    type="button"
                    role="option"
                    aria-selected={index === boundedActiveIndex}
                    aria-label={`${result.title} by ${result.artist}${
                      result.album ? `, from ${result.album}` : ""
                    }. ${index === boundedActiveIndex ? "Select this song." : "Focus this song."}`}
                    tabIndex={index === boundedActiveIndex ? 0 : -1}
                    onClick={() => {
                      if (suppressClickRef.current) return;
                      if (index === boundedActiveIndex) onSelect(result);
                      else moveTo(index, true, offset < 0 ? "backward" : "forward");
                    }}
                  >
                    {result.artwork_url ? (
                      <img src={result.artwork_url} alt="" draggable="false" />
                    ) : (
                      <span className="album-guess-placeholder" aria-hidden="true">
                        <i />
                        <b>♪</b>
                      </span>
                    )}
                    <span className="album-guess-sleeve-glint" aria-hidden="true" />
                    <span className="album-guess-spine-label" aria-hidden="true">
                      <b>{result.title}</b>
                      <i>{result.artist}</i>
                    </span>
                  </button>
                </div>
              </div>
            );
          })}
        </div>

        <button
          className="album-guess-arrow is-next"
          type="button"
          aria-label="Next song"
          onClick={() => moveTo(boundedActiveIndex + 1, true, "forward")}
        >
          <svg viewBox="0 0 24 24" aria-hidden="true">
            <path d="m9 5 7 7-7 7" />
          </svg>
        </button>
      </div>

      <header className="album-guess-copy" aria-live="polite" aria-atomic="true">
        <strong>{activeResult.title}</strong>
        <small className="album-guess-artist">{activeResult.artist}</small>
        {(activeResult.album || activeResult.release_year) && (
          <small className="album-guess-release">
            {activeResult.album}
            {activeResult.album && activeResult.release_year ? " · " : ""}
            {activeResult.release_year}
          </small>
        )}
        <PopularityScore score={activeResult.popularity_score} className="album-guess-popularity" />
      </header>
    </section>
  );
}
