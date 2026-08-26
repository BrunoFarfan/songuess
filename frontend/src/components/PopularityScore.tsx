import React, { type CSSProperties } from "react";

import "./PopularityScore.css";

export type PopularityScoreProps = {
  score: number | null;
  className?: string;
};

export function normalizePopularityScore(score: number): number {
  return Math.round(Math.max(0, Math.min(100, score)));
}

export default function PopularityScore({ score, className = "" }: PopularityScoreProps) {
  if (score === null) {
    return (
      <span
        className={`popularity-score is-unavailable ${className}`.trim()}
        aria-label="Popularity score unavailable"
      >
        <span className="popularity-score__label" aria-hidden="true">
          Popularity
        </span>
        <span className="popularity-score__track" aria-hidden="true" />
        <span className="popularity-score__value" aria-hidden="true">
          N/A
        </span>
      </span>
    );
  }

  const value = normalizePopularityScore(score);
  const style = { "--popularity-value": `${value}%` } as CSSProperties;

  return (
    <span
      className={`popularity-score ${className}`.trim()}
      aria-label={`Popularity score: ${value} out of 100`}
    >
      <span className="popularity-score__label" aria-hidden="true">
        Popularity
      </span>
      <span className="popularity-score__track" style={style} aria-hidden="true">
        <span />
      </span>
      <span className="popularity-score__value" aria-hidden="true">
        {value}
        <span>/100</span>
      </span>
    </span>
  );
}
