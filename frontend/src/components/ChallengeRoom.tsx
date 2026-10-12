import React, { useEffect, useState } from "react";
import type { RoomController } from "./useRoom";
import "./ChallengeRoom.css";
export default function ChallengeRoom({ room }: { room: RoomController }) {
  const [copiedAt, setCopiedAt] = useState<number | null>(null);
  const [expandedPlayer, setExpandedPlayer] = useState<string | null>(null);
  useEffect(() => {
    if (copiedAt === null) return;
    const timer = window.setTimeout(() => setCopiedAt(null), 2000);
    return () => window.clearTimeout(timer);
  }, [copiedAt]);
  const summary = room.summary;
  const complete = room.state && !room.state.current_round;
  function showMyResults() {
    setExpandedPlayer(summary?.leaderboard.find((entry) => entry.is_you)?.player_id ?? null);
  }
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
                <button type="button" onClick={showMyResults}>
                  View my results
                </button>
              )}
            </>
          ) : summary.is_member ? (
            <button
              className="flow-primary"
              type="button"
              disabled={room.pending}
              onClick={complete ? showMyResults : () => void room.resume()}
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
          <div className="challenge-standings">
            <div className="challenge-standings-heading">
              <h2>Standings</h2>
              <button type="button" onClick={() => void room.refresh()}>
                Refresh
              </button>
            </div>
            {summary.leaderboard.length ? (
              <ol>
                {summary.leaderboard.map((entry) => {
                  const expanded = expandedPlayer === entry.player_id;
                  const panelId = `player-songs-${entry.player_id}`;
                  return (
                    <li key={entry.player_id} className={entry.is_you ? "is-you" : ""}>
                      <button
                        className="challenge-player-toggle"
                        type="button"
                        aria-expanded={expanded}
                        aria-controls={panelId}
                        onClick={() => setExpandedPlayer(expanded ? null : entry.player_id)}
                      >
                        <b className="challenge-player-rank">{entry.rank}</b>
                        <span className="challenge-player-name">
                          <strong>
                            {entry.display_name}
                            {entry.is_you ? " (you)" : ""}
                          </strong>
                          <small>
                            {entry.correct_count} correct · {entry.completed_count}/
                            {summary.song_count} played
                          </small>
                        </span>
                        <strong>{entry.points} pts</strong>
                        <span className="challenge-player-chevron" aria-hidden="true">
                          ⌄
                        </span>
                      </button>
                      <div
                        id={panelId}
                        aria-hidden={!expanded}
                        inert={!expanded}
                        className="challenge-player-details"
                      >
                        <div className="challenge-player-details-inner">
                          <ol
                            className="challenge-player-songs"
                            aria-label={`Song scores for ${entry.display_name}`}
                          >
                            {entry.songs.map((song) => {
                              const reveal =
                                room.state && song.position <= room.state.completed_count
                                  ? room.state.results.find(
                                      (item) => item.position === song.position,
                                    )?.reveal
                                  : undefined;
                              return (
                                <li key={song.position}>
                                  <span className="challenge-song-number">
                                    {String(song.position).padStart(2, "0")}
                                  </span>
                                  {reveal?.artwork_url && (
                                    <img
                                      src={reveal.artwork_url}
                                      alt=""
                                      width="40"
                                      height="40"
                                      loading="lazy"
                                    />
                                  )}
                                  <span className="challenge-song-name">
                                    <strong>{reveal?.title ?? `Song ${song.position}`}</strong>
                                    <small>
                                      {reveal ? `${reveal.artist} · ` : ""}
                                      {song.result === null
                                        ? "Not played"
                                        : song.result === "not_guessed"
                                          ? "Not guessed"
                                          : `Clue ${song.result.slice(-1)}`}
                                    </small>
                                  </span>
                                  <b>{song.points === null ? "—" : `${song.points} pts`}</b>
                                </li>
                              );
                            })}
                          </ol>
                        </div>
                      </div>
                    </li>
                  );
                })}
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
