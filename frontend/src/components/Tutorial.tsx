import React, { useEffect, useRef, useState } from "react";
import "./Tutorial.css";

export const TUTORIAL_STORAGE_KEY = "songuess:tutorial-seen:v1";
export function hasSeenTutorial(storage: Pick<Storage, "getItem">) {
  return storage.getItem(TUTORIAL_STORAGE_KEY) === "true";
}
export function rememberTutorial(storage: Pick<Storage, "setItem">) {
  storage.setItem(TUTORIAL_STORAGE_KEY, "true");
}

export default function Tutorial({ onDismiss }: { onDismiss: () => void }) {
  const [leaving, setLeaving] = useState(false);
  const dialogRef = useRef<HTMLDivElement>(null);
  const startRef = useRef<HTMLButtonElement>(null);
  const timerRef = useRef<number | null>(null);

  function dismiss() {
    if (leaving) return;
    setLeaving(true);
    try {
      rememberTutorial(window.localStorage);
    } catch {
      /* Storage may be blocked. */
    }
    timerRef.current = window.setTimeout(onDismiss, 420);
  }

  useEffect(() => {
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    startRef.current?.focus();
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "Tab") return;
      const controls = dialogRef.current?.querySelectorAll<HTMLElement>(
        'button:not([disabled]), [href], [tabindex]:not([tabindex="-1"])',
      );
      if (!controls?.length) return;
      const first = controls[0];
      const last = controls[controls.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => {
      document.body.style.overflow = previousOverflow;
      window.removeEventListener("keydown", onKeyDown);
      if (timerRef.current !== null) window.clearTimeout(timerRef.current);
    };
  }, []);

  return (
    <div className={`tutorial-backdrop${leaving ? " is-leaving" : ""}`} role="presentation">
      <div
        ref={dialogRef}
        className="tutorial-sleeve"
        role="dialog"
        aria-modal="true"
        aria-labelledby="tutorial-title"
        aria-describedby="tutorial-intro"
      >
        <div className="tutorial-heading">
          <div>
            <span className="eyebrow">Drop the needle</span>
            <h2 id="tutorial-title">Name that song.</h2>
          </div>
          <span className="tutorial-mini-record" aria-hidden="true">
            <i />
          </span>
          <p id="tutorial-intro">
            Hear a one-second opening. Guess early for more points, or unlock a longer clue when you
            need it.
          </p>
        </div>
        <ol className="tutorial-tracklist">
          <li>
            <span>01</span>
            <div>
              <strong>Listen</strong>
              <p>Tap the record to play the current clue as often as you like.</p>
            </div>
          </li>
          <li>
            <span>02</span>
            <div>
              <strong>Find your answer</strong>
              <p>Search, then swipe through the album sleeves and choose one to guess.</p>
            </div>
          </li>
          <li>
            <span>03</span>
            <div>
              <strong>Protect your score</strong>
              <p>
                Each wrong guess or next clue spends a star. Six stars means six possible points.
              </p>
            </div>
          </li>
        </ol>
        <div className="tutorial-footer">
          <button ref={startRef} className="tutorial-start" type="button" onClick={dismiss}>
            Start listening
          </button>
        </div>
      </div>
    </div>
  );
}
