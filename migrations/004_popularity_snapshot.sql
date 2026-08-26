PRAGMA foreign_keys = OFF;

CREATE TABLE songs_with_popularity_snapshot (
    id INTEGER PRIMARY KEY,
    title TEXT NOT NULL CHECK (length(trim(title)) > 0),
    artist TEXT NOT NULL CHECK (length(trim(artist)) > 0),
    album TEXT,
    release_year INTEGER NOT NULL CHECK (release_year BETWEEN 1800 AND 2200),
    popularity_score INTEGER CHECK (
        popularity_score IS NULL OR popularity_score BETWEEN 0 AND 100
    ),
    listener_count INTEGER CHECK (listener_count IS NULL OR listener_count >= 0),
    listen_count INTEGER CHECK (listen_count IS NULL OR listen_count >= 0),
    popularity_fetched_at TEXT,
    popularity_source TEXT CHECK (
        popularity_source IS NULL OR popularity_source = 'listenbrainz_recording_popularity'
    ),
    popularity_status TEXT NOT NULL DEFAULT 'unavailable' CHECK (
        popularity_status IN (
            'complete',
            'listeners_only',
            'listens_only',
            'unavailable',
            'missing_response',
            'legacy_discovery'
        )
    ),
    musicbrainz_id TEXT UNIQUE,
    apple_track_id TEXT UNIQUE,
    preview_url TEXT NOT NULL CHECK (length(trim(preview_url)) > 0),
    artwork_url TEXT,
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1))
);

INSERT INTO songs_with_popularity_snapshot (
    id,
    title,
    artist,
    album,
    release_year,
    popularity_score,
    listener_count,
    listen_count,
    popularity_fetched_at,
    popularity_source,
    popularity_status,
    musicbrainz_id,
    apple_track_id,
    preview_url,
    artwork_url,
    enabled
)
SELECT
    id,
    title,
    artist,
    album,
    release_year,
    popularity_score,
    listener_count,
    listen_count,
    NULL,
    NULL,
    'legacy_discovery',
    musicbrainz_id,
    apple_track_id,
    preview_url,
    artwork_url,
    enabled
FROM songs;

DROP TABLE songs;
ALTER TABLE songs_with_popularity_snapshot RENAME TO songs;

CREATE INDEX idx_songs_eligibility
    ON songs(enabled, release_year, popularity_score);
CREATE INDEX idx_songs_search
    ON songs(title COLLATE NOCASE, artist COLLATE NOCASE);

PRAGMA foreign_keys = ON;
