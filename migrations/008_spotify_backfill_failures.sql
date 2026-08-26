CREATE TABLE spotify_backfill_failures (
    song_id INTEGER PRIMARY KEY REFERENCES songs(id) ON DELETE CASCADE,
    status TEXT NOT NULL CHECK (length(trim(status)) > 0),
    match_method TEXT NOT NULL CHECK (
        match_method IN ('existing_url', 'isrc', 'exact_metadata', 'browser_batch')
    ),
    attempted_at TEXT NOT NULL
);

CREATE INDEX idx_spotify_backfill_failures_status
    ON spotify_backfill_failures(status, song_id);
