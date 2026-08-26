# Follow-up ideas

## 1. Filter by artist

Add a searchable, multi-select artist filter to the game setup alongside genre, country, year, and popularity.

- Search the catalog's available artists by name and let the player select one or more suggestions.
- Interpret multiple selected artists as **any of these artists**.
- Combine the artist selection with the other filter groups using **AND**. For example, a round may be restricted to songs by Radiohead or Muse that also fall within the selected years and genres.
- Preserve selected artists when starting the next song or returning through **Change filters**.
- Keep this separate from the in-round guess search, which should continue to search the entire eligible song catalog by title or artist.
- Show the existing no-matching-songs state when the complete filter combination has no results.

The initial implementation may match exact catalog artist-credit strings. Longer-term, artist credits should be normalized so a collaboration can be found through any individual credited artist instead of only through its combined display name.

## 2. Clue-based statistics and leaderboards

Record every completed round as exactly one of the following results:

- Correct on clue 1 (1 second)
- Correct on clue 2 (2 seconds)
- Correct on clue 3 (4 seconds)
- Correct on clue 4 (7 seconds)
- Correct on clue 5 (11 seconds)
- Correct on clue 6 (15 seconds)
- Not guessed

Exhausting all attempts and giving up both count as **not guessed**. Wrong guesses and skips should not create separate scoring categories; they only determine which clue the player eventually reaches.

Personal statistics should include:

- Total songs played
- Number and percentage guessed correctly
- Number and percentage not guessed
- A distribution of correct answers across clues 1–6
- Average clue number for correctly guessed songs
- Current and best correct-answer streak

Solo statistics can initially be persisted in browser storage without requiring an account. Shared or cross-device statistics will require persistent player identity and backend storage.

For competitions covering multiple songs, use this scoring system:

| Result | Points |
| --- | ---: |
| Clue 1 | 6 |
| Clue 2 | 5 |
| Clue 3 | 4 |
| Clue 4 | 3 |
| Clue 5 | 2 |
| Clue 6 | 1 |
| Not guessed | 0 |

Rank players by total points. Break ties by, in order:

1. Most songs guessed correctly.
2. Most clue-1 solves, then most clue-2 solves, continuing through clue 6.
3. If players remain equal, declare a tie.

Use the same result and scoring model for solo statistics, multiplayer rooms, and the eventual daily mode so their definitions do not drift apart.

## 3. Reveal within the main game view

Make the answer reveal feel like the conclusion of the current game surface instead of replacing it with a visually separate reveal card.

When a round ends because of a correct guess, exhausted attempts, or a confirmed reveal:

1. Stop the clue audio and fetch the full song data.
2. Keep the main gameplay layout mounted.
3. Flip the vinyl in three dimensions, turning its reverse side into the album artwork.
4. Reveal the outcome and the existing song information: title, artist, album, release year, and genres.
5. Smoothly collapse or fade out the guess search, selected guess, and previous wrong guesses.
6. Transform the existing bottom controls in place:
   - **Reveal** becomes **Change filters**.
   - **Play/Pause clue** becomes **Play/Pause full preview**.
   - **Next clue** becomes **Next song**.

Do not autoplay the full preview. Preserve the volume control and keep the page geometry as stable as practical, especially on mobile. Continue to show whether the outcome was **Correct**, **Missed**, or **Revealed**.

If reveal data fails to load, retain a usable retry, next-song, or change-filter path rather than leaving the game stuck. When the user prefers reduced motion, replace the 3D flip and larger movements with a short crossfade.

## 4. Raised buttons that press down

Apply a consistent physical-button interaction model across the interface:

- At rest, a button appears raised above its surface with an offset solid shadow.
- Hover or keyboard focus may lift or highlight it slightly further without changing its layout footprint.
- While actively pressed, the button moves down toward the underlying surface and its offset shadow shrinks or disappears.
- On release, it returns quickly to its raised resting position.
- Disabled buttons remain visually muted and do not animate as interactive controls.

Use the same model for primary and secondary actions, circular dock controls, modal buttons, search results, chips, and the vinyl rewind button. Keep press feedback short—approximately 100–160 ms—and preserve visible keyboard focus, touch feedback, and reduced-motion behavior.

## 5. Asynchronous multiplayer rooms

Add asynchronous rooms or sessions in which participants do not need to be online at the same time but always play the same frozen, ordered set of songs.

The intended room flow is:

1. A player creates a room and chooses its filters, number of songs, and optional completion deadline.
2. The server selects and freezes the ordered song set when the room is created or started.
3. The creator shares a join code or link.
4. Players join with a display name or account and complete the challenge independently.
5. Each player may complete each song's normal six-clue round exactly once.
6. The room records every player's progress and clue-based result for each song.
7. When all players finish or the deadline passes, final standings use the scoring and tie-breaking rules from the clue-based leaderboard.

“One attempt at each song” means one complete six-clue round per player and song, not one literal song guess. A player cannot restart a completed song to improve its score.

A room should persist:

- Owner and join code
- Shareable join link
- Filters and frozen song order
- Number of songs
- Participants and display names
- Per-player progress
- One result per player and song
- Optional deadline
- Room state, such as lobby, active, or completed
- Final scores and ranking

Partial standings should be hidden from a player until they finish their own challenge, or until the room closes, so other results do not influence play.

The first version may be a casual, honor-system competition. The current API exposes preview URLs and reveal data, so a cheating-resistant version would additionally require server-controlled round progression, guarded reveal access, and stronger player identity.

Design the shared result and session model so it can later support two modes:

### Daily mode

- One globally selected song for each calendar day
- The same song for every player
- One completed result per player per day
- Daily results, history, and streaks
- A clearly defined day boundary, initially UTC unless another product-wide timezone is chosen
- Answer and standings hidden until the player finishes or the daily period closes

### Standard room mode

- A configurable set of songs shared by everyone in the room
- Each player completes every song once, on their own schedule and before any deadline
- Final ranking across the entire song set

## 6. Fuzzy song and artist search while guessing

Make song and artist search during guessing fuzzy and typo-tolerant.

- For every query that meets the minimum input length, rank the enabled catalog by similarity and return the 10 closest eligible matches even when their similarity scores are low.
- Do not apply a minimum similarity-score cutoff.
- Prefer exact and prefix matches when available, then rank approximate matches using normalized song titles, artist names, and their combined text.
- Ignore differences in capitalization, accents, punctuation, and repeated whitespace where practical.
- Exclude previously guessed songs before applying the 10-result limit, so the player still sees 10 choices whenever at least 10 eligible songs remain.
- Use deterministic tie-breaking so the same query against the same catalog produces a stable order.
- Continue showing each result's song title, artist, and artwork.

RapidFuzz is the preferred implementation for the current Python backend, but the behavioral contract matters more than the particular library.

## Recommended implementation order

1. Raised button interaction
2. Artist filter
3. In-place reveal transition
4. Local round history and personal statistics
5. Persistent shared scoring model
6. Asynchronous multiplayer rooms
7. Daily challenge
8. Fuzzy guess search may be implemented independently whenever search quality becomes a priority
