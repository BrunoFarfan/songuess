import React, { type AnimationEvent, type ReactNode, useEffect, useState } from "react";

import PopularityScore from "./PopularityScore";
import "./VinylSleeveReveal.css";

export type VinylSleeveRevealOutcome = "correct" | "failed" | "gave_up" | null;

export type VinylSleeveRevealSong = {
  title: string;
  artist: string;
  album?: string | null;
  release_year?: number | null;
  artwork_url?: string | null;
  popularity_score: number | null;
  genres?: string[];
  apple_music_url?: string | null;
  spotify_url?: string | null;
};

export type VinylSleeveRevealProps = {
  /** Pass the existing vinyl control here so it stays mounted and visually unchanged. */
  children: ReactNode;
  revealed: boolean;
  outcome: VinylSleeveRevealOutcome;
  song: VinylSleeveRevealSong | null;
  loading?: boolean;
  error?: string;
  onRetry?: () => void;
  className?: string;
};

const outcomeLabels: Record<Exclude<VinylSleeveRevealOutcome, null>, string> = {
  correct: "Correct",
  failed: "Missed",
  gave_up: "Revealed",
};

export default function VinylSleeveReveal({
  children,
  revealed,
  outcome,
  song,
  loading = false,
  error = "",
  onRetry,
  className = "",
}: VinylSleeveRevealProps) {
  const [revealSettled, setRevealSettled] = useState(false);

  useEffect(() => {
    if (!revealed) {
      setRevealSettled(false);
      return;
    }

    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
      setRevealSettled(true);
      return;
    }

    const fallback = window.setTimeout(() => setRevealSettled(true), 1100);
    return () => window.clearTimeout(fallback);
  }, [revealed]);

  function settleReveal(event: AnimationEvent<HTMLElement>) {
    if (event.animationName === "vinyl-sleeve-enter") setRevealSettled(true);
  }

  const outcomeClass = outcome ? ` outcome-${outcome}` : " outcome-pending";
  const rootClass = [
    "vinyl-sleeve-reveal",
    revealed ? "is-revealed" : "",
    revealSettled ? "is-settled" : "",
    outcomeClass,
    className,
  ]
    .filter(Boolean)
    .join(" ");

  const outcomeLabel = outcome ? outcomeLabels[outcome] : "Answer";

  return (
    <section
      className={rootClass}
      aria-live="polite"
      aria-busy={revealed && loading}
      aria-label={revealed && song ? `${outcomeLabel}: ${song.title} by ${song.artist}` : undefined}
    >
      <div className="vinyl-sleeve-reveal__stage">
        <div className="vinyl-sleeve-reveal__spine" aria-hidden="true" />
        <div className="vinyl-sleeve-reveal__record">{children}</div>

        <figure
          className="vinyl-sleeve-reveal__sleeve"
          aria-hidden={!revealed}
          onAnimationEnd={settleReveal}
        >
          <div className="vinyl-sleeve-reveal__artwork">
            {song?.artwork_url ? (
              <img src={song.artwork_url} alt={revealed ? `Album artwork for ${song.title}` : ""} />
            ) : (
              <span className="vinyl-sleeve-reveal__artwork-placeholder" aria-hidden="true">
                <span>♪</span>
              </span>
            )}
          </div>
        </figure>

        {revealed && outcome === "correct" && (
          <div className="vinyl-sleeve-reveal__confetti" aria-hidden="true">
            {Array.from({ length: 14 }, (_, index) => (
              <span key={index} />
            ))}
          </div>
        )}
      </div>

      {revealed && (
        <div className="vinyl-sleeve-reveal__caption">
          <span className="vinyl-sleeve-reveal__outcome">{outcomeLabel}</span>

          {loading && !song && (
            <p className="vinyl-sleeve-reveal__loading">Pulling the sleeve notes…</p>
          )}

          {error && !song && (
            <div className="vinyl-sleeve-reveal__error">
              <p>{error}</p>
              {onRetry && (
                <button type="button" onClick={onRetry}>
                  Retry details
                </button>
              )}
            </div>
          )}

          {song && (
            <div className="vinyl-sleeve-reveal__song">
              <h1>{song.title}</h1>
              <p>{song.artist}</p>
              {(song.album || song.release_year) && (
                <small>{[song.album, song.release_year].filter(Boolean).join(" · ")}</small>
              )}
              <PopularityScore
                score={song.popularity_score}
                className="vinyl-sleeve-reveal__popularity"
              />
              {song.genres && song.genres.length > 0 && (
                <span className="vinyl-sleeve-reveal__genres">{song.genres.join(" · ")}</span>
              )}
            </div>
          )}

          {song && (song.apple_music_url || song.spotify_url) && (
            <nav className="vinyl-sleeve-reveal__streaming-links" aria-label="Listen to this song">
              {song.apple_music_url && (
                <a
                  className="is-apple-music"
                  href={song.apple_music_url}
                  target="_blank"
                  rel="noopener noreferrer"
                >
                  <span aria-hidden="true">♪</span>
                  Listen on Apple Music
                </a>
              )}
              {song.spotify_url && (
                <a
                  className="is-spotify"
                  href={song.spotify_url}
                  target="_blank"
                  rel="noopener noreferrer"
                >
                  <svg viewBox="0 0 24 24" aria-hidden="true">
                    <path d="M5 8.2c4.7-1.4 9.8-1 14 .9" />
                    <path d="M6.1 12c3.9-1 8.2-.7 11.7.8" />
                    <path d="M7.2 15.7c3.1-.7 6.4-.4 9.2.8" />
                  </svg>
                  Listen on Spotify
                </a>
              )}
            </nav>
          )}
        </div>
      )}
    </section>
  );
}
