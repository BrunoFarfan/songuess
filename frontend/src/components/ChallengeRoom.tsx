import React, { useEffect, useState } from "react";
import { pointsForResult } from "../lib/roundHistory";
import type { RoomController } from "./useRoom";
import "./ChallengeRoom.css";
export default function ChallengeRoom({ room }: { room: RoomController }) {
  const [copiedAt, setCopiedAt] = useState<number | null>(null);
  useEffect(() => {
    if (copiedAt === null) return;
    const timer = window.setTimeout(() => setCopiedAt(null), 2000);
    return () => window.clearTimeout(timer);
  }, [copiedAt]);
  const summary = room.summary;
  const complete = room.state && !room.state.current_round;
  return (
    <section className="challenge-room" aria-labelledby="challenge-heading">
      <header className="flow-heading">
        <span className="eyebrow">Challenge · {room.code}</span>
        <h1 id="challenge-heading" tabIndex={-1} data-flow-heading>
          {summary?.status === "closed"
            ? "Challenge results"
            : complete
              ? "Challenge complete"
              : "Challenge room"}
        </h1>
        <p>
          {summary?.status === "closed"
            ? "This challenge has ended. Compare the final scores below."
            : "Everyone gets the same songs. Play before the deadline and compare scores here."}
        </p>
      </header>
      {room.error && (
        <p className="inline-error" role="alert">
          {room.error}
        </p>
      )}
      {!summary ? (
        <>
          <p>Load the challenge to see its songs, deadline and standings.</p>
          <button type="button" onClick={() => void room.refresh()}>
            Retry loading challenge
          </button>
        </>
      ) : (
        <>
          <div className="challenge-facts">
            <strong>
              {summary.song_count} {summary.song_count === 1 ? "song" : "songs"}
            </strong>
            <span>{summary.participant_count} / 50 players</span>
            <span>
              {summary.status === "closed"
                ? "Challenge closed"
                : `Closes ${new Date(summary.closes_at).toLocaleString()}`}
            </span>
          </div>
          <p className="challenge-filters">
            {[
              summary.filters.artist_id ? "Selected artist" : "All artists",
              summary.filters.genres.join(" · ") || "All genres",
              summary.filters.countries.join(" · ") || "All countries",
              `${summary.filters.year_min ?? "Any year"} – ${summary.filters.year_max ?? "now"}`,
              `Popularity ${summary.filters.popularity_min ?? 0}–${summary.filters.popularity_max ?? 100}`,
            ].join(" / ")}
          </p>
          <div className="challenge-invite">
            <label htmlFor="challenge-invite">Invite link</label>
            <input
              id="challenge-invite"
              readOnly
              value={
                typeof location === "undefined"
                  ? `/?room=${room.code}`
                  : `${location.origin}/?room=${room.code}`
              }
              onFocus={(event) => event.target.select()}
            />
            <button
              type="button"
              onClick={() => {
                void navigator.clipboard
                  .writeText(`${location.origin}/?room=${room.code}`)
                  .then(() => setCopiedAt(Date.now()))
                  .catch(() => setCopiedAt(null));
              }}
            >
              {copiedAt !== null ? "Copied" : "Copy link"}
            </button>
          </div>
          {room.state && (
            <p>
              Your record: <strong>{room.state.total_points} points</strong> ·{" "}
              {room.state.completed_count} / {summary.song_count} songs
            </p>
          )}
          {summary.status === "closed" ? (
            <>
              <p>
                Play has ended. Standings remain available until{" "}
                {new Date(summary.delete_at).toLocaleDateString()}.
              </p>
              {summary.is_member && (
                <button type="button" disabled={room.pending} onClick={() => void room.resume()}>
                  View my results
                </button>
              )}
            </>
          ) : summary.is_member ? (
            <button
              className="flow-primary"
              type="button"
              disabled={room.pending}
              onClick={() => void room.resume()}
            >
              {complete ? "View my results" : room.pending ? "Loading…" : "Start listening"}
            </button>
          ) : summary.participant_count >= 50 ? (
            <p>This challenge is full.</p>
          ) : (
            <form
              className="challenge-join"
              onSubmit={(event) => {
                event.preventDefault();
                void room.join();
              }}
            >
              <label htmlFor="challenge-name">Your name</label>
              <input
                id="challenge-name"
                value={room.name}
                maxLength={24}
                required
                onChange={(event) => room.setName(event.target.value)}
                autoComplete="nickname"
              />
              <button
                className="flow-primary"
                disabled={room.pending || !room.validName}
                type="submit"
              >
                {room.pending ? "Joining…" : "Join & play"}
              </button>
            </form>
          )}
          {room.state && room.state.results.length > 0 && (
            <details className="challenge-results" open>
              <summary>Your songs</summary>
              <ol>
                {room.state.results.map((item) => (
                  <li key={item.position}>
                    {item.reveal.artwork_url && (
                      <img src={item.reveal.artwork_url} alt="" width="48" height="48" />
                    )}
                    <span>
                      <strong>
                        {item.position}. {item.reveal.title}
                      </strong>
                      <small>
                        {item.reveal.artist} ·{" "}
                        {item.result === "not_guessed"
                          ? "Not guessed"
                          : `Clue ${item.result.slice(-1)}`}
                      </small>
                    </span>
                    <b>{pointsForResult(item.result)} pts</b>
                  </li>
                ))}
              </ol>
            </details>
          )}
          <div className="challenge-standings">
            <div className="challenge-standings-heading">
              <h2>Standings</h2>
              <button type="button" onClick={() => void room.refresh()}>
                Refresh
              </button>
            </div>
            {summary.leaderboard.length ? (
              <ol>
                {summary.leaderboard.map((entry, index) => (
                  <li key={index} className={entry.is_you ? "is-you" : ""}>
                    <b>{entry.rank}</b>
                    <span>
                      <strong>
                        {entry.display_name}
                        {entry.is_you ? " (you)" : ""}
                      </strong>
                      <small>
                        {entry.correct_count} correct · {entry.completed_count}/{summary.song_count}{" "}
                        played
                      </small>
                    </span>
                    <strong>{entry.points} pts</strong>
                  </li>
                ))}
              </ol>
            ) : (
              <p>No scores yet. The first record is yours to set.</p>
            )}
          </div>
        </>
      )}
      <button className="challenge-home" type="button" onClick={room.multiplayer}>
        ← Back to multiplayer
      </button>
    </section>
  );
}
