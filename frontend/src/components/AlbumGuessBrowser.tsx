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

import "./AlbumGuessBrowser.css";

export type AlbumGuessOption = {
  id: number;
  title: string;
  artist: string;
  artwork_url: string | null;
  album: string | null;
  release_year: number;
  popularity_score: number | null;
  searchIndex?: number;
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
  totalCount?: number;
  excludedIndexes?: number[];
  onNeedIndex?: (index: number) => void | Promise<void>;
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
const SWIPE_MIN_DISTANCE = 14;
const SWIPE_MIN_VELOCITY = 0.14;
const SWIPE_STEP_DISTANCE = 56;
const SWIPE_PROJECTION_MS = 72;
const MAX_SWIPE_STEPS = 7;
const SWIPE_AXIS_LOCK_DISTANCE = 8;
const ALBUMS_EACH_SIDE = Math.floor(VISIBLE_ALBUM_COUNT / 2);

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
  appliedDelta: number;
  axis: "horizontal" | "vertical" | null;
};

export function wrapAlbumIndex(index: number, resultCount: number): number {
  if (resultCount <= 0) return 0;
  return ((index % resultCount) + resultCount) % resultCount;
}

function advanceAlbumIndex(
  activeIndex: number,
  delta: number,
  resultCount: number,
  excludedIndexes: ReadonlySet<number>,
): number {
  if (resultCount <= 0) return 0;
  if (delta === 0) return wrapAlbumIndex(activeIndex, resultCount);
  const direction = Math.sign(delta);
  let nextIndex = wrapAlbumIndex(activeIndex, resultCount);
  let remaining = Math.abs(delta);
  let inspected = 0;
  while (remaining > 0 && inspected < resultCount * Math.max(1, Math.abs(delta))) {
    nextIndex = wrapAlbumIndex(nextIndex + direction, resultCount);
    inspected += 1;
    if (!excludedIndexes.has(nextIndex)) remaining -= 1;
  }
  return nextIndex;
}

export function visibleAlbumIndexes(
  activeIndex: number,
  resultCount: number,
  excludedIndexes: ReadonlySet<number> = new Set(),
): number[] {
  const availableCount = Math.max(0, resultCount - excludedIndexes.size);
  if (availableCount === 0) return [];
  const visibleCount = Math.min(VISIBLE_ALBUM_COUNT, availableCount);
  const leftCount = Math.min(ALBUMS_EACH_SIDE, Math.floor((visibleCount - 1) / 2));
  const firstIndex = advanceAlbumIndex(
    wrapAlbumIndex(activeIndex, resultCount),
    -leftCount,
    resultCount,
    excludedIndexes,
  );
  return Array.from({ length: visibleCount }, (_, offset) =>
    advanceAlbumIndex(firstIndex, offset, resultCount, excludedIndexes),
  );
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
  totalCount = results.length,
  excludedIndexes = [],
  onNeedIndex,
}: AlbumGuessBrowserProps) {
  const [activeIndex, setActiveIndex] = useState(0);
  const [dragOffset, setDragOffset] = useState(0);
  const [isDragging, setIsDragging] = useState(false);
  const optionIdPrefix = useId();
  const cardRefs = useRef(new Map<number, HTMLButtonElement>());
  const previousQueryRef = useRef("");
  const pendingActiveIndexRef = useRef<number | null>(null);
  const activeIndexRef = useRef(0);
  const dragGestureRef = useRef<DragGesture | null>(null);
  const suppressClickRef = useRef(false);
  const onActiveChangeRef = useRef(onActiveChange);
  const resultByIndex = useMemo(
    () => new Map(results.map((result, index) => [result.searchIndex ?? index, result])),
    [results],
  );
  const resultSignature = useMemo(
    () => results.map((result, index) => `${result.searchIndex ?? index}:${result.id}`).join(":"),
    [results],
  );
  const excludedIndexSet = useMemo(() => new Set(excludedIndexes), [excludedIndexes]);
  const normalizedQuery = query.trim();

  function updateActiveIndex(nextIndex: number) {
    activeIndexRef.current = nextIndex;
    setActiveIndex(nextIndex);
  }

  useEffect(() => {
    onActiveChangeRef.current = onActiveChange;
  }, [onActiveChange]);

  useEffect(() => {
    const selectedResult = results.find(({ id }) => id === selectedId);
    const selectedIndex = selectedResult?.searchIndex;
    if (previousQueryRef.current !== normalizedQuery) {
      previousQueryRef.current = normalizedQuery;
      pendingActiveIndexRef.current = null;
      updateActiveIndex(selectedIndex ?? results[0]?.searchIndex ?? 0);
      return;
    }
    const pendingActiveIndex = pendingActiveIndexRef.current;
    if (pendingActiveIndex !== null && resultByIndex.has(pendingActiveIndex)) {
      pendingActiveIndexRef.current = null;
      updateActiveIndex(pendingActiveIndex);
      return;
    }
    if (excludedIndexSet.has(activeIndex)) {
      updateActiveIndex(advanceAlbumIndex(activeIndex, 1, totalCount, excludedIndexSet));
    }
  }, [
    activeIndex,
    excludedIndexSet,
    normalizedQuery,
    resultByIndex,
    results,
    selectedId,
    totalCount,
  ]);

  const boundedActiveIndex = wrapAlbumIndex(activeIndex, totalCount);
  const activeResult = resultByIndex.get(boundedActiveIndex);

  useEffect(() => {
    if (activeResult) onActiveChangeRef.current?.(activeResult);
  }, [activeResult]);

  useEffect(() => {
    if (!onNeedIndex || totalCount <= 0) return;
    for (let offset = -VISIBLE_ALBUM_COUNT; offset <= VISIBLE_ALBUM_COUNT; offset += 1) {
      const index = advanceAlbumIndex(boundedActiveIndex, offset, totalCount, excludedIndexSet);
      if (!resultByIndex.has(index)) void onNeedIndex(index);
    }
  }, [boundedActiveIndex, excludedIndexSet, onNeedIndex, resultByIndex, totalCount]);

  const visibleResults = useMemo(() => {
    return visibleAlbumIndexes(boundedActiveIndex, totalCount, excludedIndexSet).map(
      (index, position) => ({
        result: resultByIndex.get(index),
        index,
        offset:
          position -
          Math.floor((Math.min(VISIBLE_ALBUM_COUNT, totalCount - excludedIndexSet.size) - 1) / 2),
        position,
      }),
    );
  }, [boundedActiveIndex, excludedIndexSet, resultByIndex, resultSignature, totalCount]);

  function moveBy(delta: number, focus = false) {
    if (totalCount <= excludedIndexSet.size) return;
    const startingIndex = pendingActiveIndexRef.current ?? activeIndexRef.current;
    const nextIndex = advanceAlbumIndex(startingIndex, delta, totalCount, excludedIndexSet);
    const nextResult = resultByIndex.get(nextIndex);
    if (!nextResult) {
      pendingActiveIndexRef.current = nextIndex;
      void onNeedIndex?.(nextIndex);
      return;
    }
    pendingActiveIndexRef.current = null;
    updateActiveIndex(nextIndex);
    if (focus) {
      window.requestAnimationFrame(() => cardRefs.current.get(nextIndex)?.focus());
    }
  }

  function moveToAbsolute(index: number, focus = false) {
    const nextIndex = wrapAlbumIndex(index, totalCount);
    const nextResult = resultByIndex.get(nextIndex);
    if (!nextResult) {
      void onNeedIndex?.(nextIndex);
      return;
    }
    updateActiveIndex(nextIndex);
    if (focus) window.requestAnimationFrame(() => cardRefs.current.get(nextIndex)?.focus());
  }

  function handleKeyboard(event: KeyboardEvent<HTMLDivElement>) {
    if (event.key === "ArrowLeft") {
      event.preventDefault();
      moveBy(-1, true);
    } else if (event.key === "ArrowRight") {
      event.preventDefault();
      moveBy(1, true);
    } else if (event.key === "Home") {
      event.preventDefault();
      moveToAbsolute(0, true);
    } else if (event.key === "End") {
      event.preventDefault();
      moveToAbsolute(totalCount - 1, true);
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
      moveBy(event.key === "ArrowLeft" ? -1 : 1);
    }

    window.addEventListener("keydown", handleWindowNavigation);
    return () => window.removeEventListener("keydown", handleWindowNavigation);
  }, [boundedActiveIndex, resultSignature, results, totalCount]);

  function beginSwipe(event: ReactPointerEvent<HTMLDivElement>) {
    if (!event.isPrimary) return;
    if (event.pointerType === "mouse" && event.button !== 0) return;
    dragGestureRef.current = {
      pointerId: event.pointerId,
      startX: event.clientX,
      startY: event.clientY,
      startTime: event.timeStamp,
      latestX: event.clientX,
      latestY: event.clientY,
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
    event.preventDefault();

    const liveDelta = swipeLiveTraversalDelta(distance);
    if (liveDelta !== gesture.appliedDelta) {
      const incrementalDelta = liveDelta - gesture.appliedDelta;
      gesture.appliedDelta = liveDelta;
      moveBy(incrementalDelta);
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
      moveBy(traversalDelta - gesture.appliedDelta);
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
          onClick={() => moveBy(-1, true)}
        >
          <svg viewBox="0 0 24 24" aria-hidden="true">
            <path d="m15 5-7 7 7 7" />
          </svg>
        </button>

        <div
          className="album-guess-stage"
          role="listbox"
          aria-label={ariaLabel}
          aria-activedescendant={activeResult ? `${optionIdPrefix}-${activeResult.id}` : undefined}
          style={{ "--album-drag": `${dragOffset}px` } as CSSProperties}
          onPointerDown={beginSwipe}
          onPointerMove={updateSwipe}
          onPointerUp={finishSwipe}
          onPointerCancel={(event) => finishSwipe(event, true)}
          onLostPointerCapture={(event) => finishSwipe(event, true)}
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
              "--album-drag-active": `${dragOffset}px`,
              "--album-drag-rotation": `${dragOffset / -12}deg`,
              "--album-shelf-angle": `${side < 0 ? -68 : 68}deg`,
              "--album-shelf-mobile-angle": `${side < 0 ? -76 : 76}deg`,
              "--album-shelf-scale": 0.86,
              "--album-shelf-x": `${shelfX}rem`,
              "--album-shelf-mobile-x": `${shelfMobileX}rem`,
              "--album-shelf-y": "0.45rem",
              "--album-shelf-z": "0px",
              zIndex: VISIBLE_ALBUM_COUNT * 2 - depth * 2 - (side > 0 ? 1 : 0),
            };

            if (!result) {
              return (
                <div
                  className="album-guess-item is-page-placeholder"
                  key={index}
                  role="presentation"
                  style={style}
                >
                  <div className="album-guess-deal" role="presentation">
                    <div className="album-guess-cover" aria-hidden="true">
                      <span className="album-guess-placeholder">
                        <i />
                        <b>♪</b>
                      </span>
                    </div>
                  </div>
                </div>
              );
            }

            return (
              <div
                className={`album-guess-item${index === boundedActiveIndex ? " is-active" : ""}`}
                key={index}
                role="presentation"
                style={style}
              >
                <div className="album-guess-deal" role="presentation">
                  <button
                    ref={(node) => {
                      if (node) cardRefs.current.set(index, node);
                      else cardRefs.current.delete(index);
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
                      else moveBy(offset, true);
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
          onClick={() => moveBy(1, true)}
        >
          <svg viewBox="0 0 24 24" aria-hidden="true">
            <path d="m9 5 7 7-7 7" />
          </svg>
        </button>
      </div>

      {activeResult && (
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
        </header>
      )}
    </section>
  );
}
