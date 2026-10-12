import React, { useState, type ReactNode } from "react";
import { selectionSummary, type SetupFilters } from "./SetupWizard";
import type { RoomController } from "./useRoom";
import "./ChallengeRoom.css";

function Heading({
  kicker,
  title,
  children,
}: {
  kicker: string;
  title: string;
  children?: ReactNode;
}) {
  return (
    <header className="flow-heading">
      <span className="eyebrow">{kicker}</span>
      <h1 tabIndex={-1} data-flow-heading>
        {title}
      </h1>
      {children && <p>{children}</p>}
    </header>
  );
}

function Choice({
  number,
  title,
  description,
  multiple = false,
  onClick,
}: {
  number: string;
  title: string;
  description: string;
  multiple?: boolean;
  onClick: () => void;
}) {
  return (
    <button className="play-choice" type="button" onClick={onClick}>
      <span className="play-choice-copy">
        <span className="play-choice-number">{number}</span>
        <strong>{title}</strong>
        <span className="play-choice-description">{description}</span>
      </span>
      <span className={`play-choice-records${multiple ? " is-multiple" : ""}`} aria-hidden="true">
        <span className="play-choice-record" />
        {multiple && <span className="play-choice-record" />}
        <span className="play-choice-arrow">↗</span>
      </span>
    </button>
  );
}

export function SetupNavigation({
  label,
  onBack,
  disabled = false,
}: {
  label: string;
  onBack: () => void;
  disabled?: boolean;
}) {
  return (
    <nav className="setup-navigation" aria-label="Play setup">
      <button
        type="button"
        className="flow-back"
        onClick={onBack}
        disabled={disabled}
        aria-label={`Back from ${label}`}
      >
        <span aria-hidden="true">←</span>
      </button>
      <span>{label}</span>
    </nav>
  );
}

export function ModeSelection({
  onSolo,
  onMultiplayer,
}: {
  onSolo: () => void;
  onMultiplayer: () => void;
}) {
  return (
    <section className="play-entry" aria-label="Choose play mode">
      <Heading kicker="Drop the needle" title="Play your way.">
        Name songs from a few seconds of sound.
      </Heading>
      <div className="play-choices">
        <Choice
          number="SIDE A"
          title="Solo"
          description="An endless mix. Just you and your ears."
          onClick={onSolo}
        />
        <Choice
          number="SIDE B"
          title="Multiplayer"
          description="The same songs for friends. A shared leaderboard."
          multiple
          onClick={onMultiplayer}
        />
      </div>
    </section>
  );
}

export function MultiplayerMenu({ room }: { room: RoomController }) {
  return (
    <section className="play-entry" aria-label="Multiplayer setup">
      <SetupNavigation label="Multiplayer" onBack={room.home} />
      <Heading kicker="A little friendly competition" title="Share the mix.">
        Everyone plays on their own schedule. Compare scores when you’re ready.
      </Heading>
      <div className="play-choices">
        <Choice
          number="01 / MAKE A MIX"
          title="Create challenge"
          description="Choose the songs, then invite your friends."
          multiple
          onClick={() => room.setMode("create")}
        />
        <Choice
          number="02 / GOT A CODE?"
          title="Join challenge"
          description="Step into a mix someone has shared with you."
          onClick={() => room.setMode("join")}
        />
      </div>
    </section>
  );
}

export function JoinChallenge({ room }: { room: RoomController }) {
  const [code, setCode] = useState("");
  return (
    <section className="play-entry" aria-label="Join a challenge">
      <SetupNavigation label="Join challenge" onBack={room.multiplayer} />
      <Heading kicker="Your invitation" title="You’re on the list.">
        Enter the code your friend shared to see the mix and join.
      </Heading>
      <form
        className="challenge-form"
        onSubmit={(event) => {
          event.preventDefault();
          room.open(code);
        }}
      >
        <label htmlFor="challenge-code">Challenge code</label>
        <input
          id="challenge-code"
          className="challenge-code-input"
          value={code}
          maxLength={8}
          minLength={8}
          required
          autoComplete="off"
          autoCapitalize="characters"
          spellCheck={false}
          placeholder="ABCDEFGH"
          onChange={(event) => setCode(event.target.value.toUpperCase())}
        />
        {room.error && (
          <p className="inline-error" role="alert">
            {room.error}
          </p>
        )}
        <button className="flow-primary" type="submit" disabled={code.trim().length !== 8}>
          Find challenge <span aria-hidden="true">→</span>
        </button>
      </form>
    </section>
  );
}

export function ChallengeDetails({
  room,
  filters,
}: {
  room: RoomController;
  filters: SetupFilters;
}) {
  return (
    <section className="play-entry challenge-details" aria-label="Challenge details">
      <SetupNavigation
        label="Challenge details"
        onBack={() => room.setMode("create")}
        disabled={room.pending}
      />
      <Heading kicker="Your mix is ready" title="Make it a challenge.">
        Give your friends the same songs. They can join and play anytime before it closes.
      </Heading>
      <dl className="challenge-mix-summary">
        <div>
          <dt>Your mix</dt>
          <dd>
            {[
              filters.artist?.name,
              selectionSummary(filters.genres),
              selectionSummary(filters.countries),
            ]
              .filter(Boolean)
              .join(" / ") || "All artists, genres & countries"}
          </dd>
        </div>
        <div>
          <dt>Years</dt>
          <dd>
            {filters.yearMin}–{filters.yearMax}
          </dd>
        </div>
        <div>
          <dt>Popularity</dt>
          <dd>
            {filters.popularityMin}–{filters.popularityMax}
          </dd>
        </div>
      </dl>
      <form
        className="challenge-form"
        onSubmit={(event) => {
          event.preventDefault();
          void room.create(filters);
        }}
      >
        <label htmlFor="creator-name">Your name</label>
        <input
          id="creator-name"
          value={room.name}
          required
          maxLength={24}
          autoComplete="nickname"
          placeholder="What should your friends call you?"
          onChange={(event) => room.setName(event.target.value)}
          disabled={room.pending}
        />
        <div className="challenge-settings">
          <label htmlFor="challenge-song-count">
            Songs in the mix
            <input
              id="challenge-song-count"
              type="number"
              min={1}
              max={20}
              step={1}
              required
              value={room.songCount}
              onChange={(event) => room.setSongCount(Number(event.target.value))}
              disabled={room.pending}
            />
          </label>
          <label htmlFor="challenge-duration">
            Hours to play
            <input
              id="challenge-duration"
              type="number"
              min={1}
              max={24}
              step={1}
              required
              value={room.duration}
              onChange={(event) => room.setDuration(Number(event.target.value))}
              disabled={room.pending}
            />
          </label>
        </div>
        <p className="challenge-form-note">
          Starts as soon as you create it. Results stay available for seven days after it closes.
        </p>
        {room.error && (
          <p className="inline-error" role="alert">
            {room.error}
          </p>
        )}
        <button
          className="flow-primary"
          type="submit"
          disabled={
            room.pending ||
            !room.validName ||
            !Number.isInteger(room.songCount) ||
            room.songCount < 1 ||
            room.songCount > 20 ||
            !Number.isInteger(room.duration) ||
            room.duration < 1 ||
            room.duration > 24
          }
        >
          {room.pending ? "Creating your mix…" : "Create & invite friends"}
          <span aria-hidden="true">→</span>
        </button>
      </form>
    </section>
  );
}
