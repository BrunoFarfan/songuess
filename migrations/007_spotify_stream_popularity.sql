PRAGMA foreign_keys = OFF;

CREATE TABLE songs_with_spotify_popularity (
    id INTEGER PRIMARY KEY,
    title TEXT NOT NULL CHECK (length(trim(title)) > 0),
    artist TEXT NOT NULL CHECK (length(trim(artist)) > 0),
    album TEXT,
    release_year INTEGER NOT NULL CHECK (release_year BETWEEN 1800 AND 2200),
    popularity_score INTEGER CHECK (
        popularity_score IS NULL OR popularity_score BETWEEN 0 AND 100
    ),
    stream_count INTEGER CHECK (stream_count IS NULL OR stream_count >= 0),
    stream_count_fetched_at TEXT,
    stream_count_source TEXT CHECK (
        stream_count_source IS NULL OR stream_count_source = 'spotify_web_hydration'
    ),
    stream_count_status TEXT NOT NULL DEFAULT 'pending' CHECK (
        stream_count_status IN ('pending', 'complete', 'missing_link', 'hydration_failed')
    ),
    musicbrainz_id TEXT UNIQUE,
    apple_track_id TEXT UNIQUE,
    preview_url TEXT NOT NULL CHECK (length(trim(preview_url)) > 0),
    artwork_url TEXT,
    apple_music_url TEXT,
    spotify_url TEXT,
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1))
);

INSERT INTO songs_with_spotify_popularity (
    id,
    title,
    artist,
    album,
    release_year,
    popularity_score,
    stream_count,
    stream_count_fetched_at,
    stream_count_source,
    stream_count_status,
    musicbrainz_id,
    apple_track_id,
    preview_url,
    artwork_url,
    apple_music_url,
    spotify_url,
    enabled
)
SELECT
    id,
    title,
    artist,
    album,
    release_year,
    CASE WHEN spotify_stream_count IS NOT NULL THEN popularity_score ELSE NULL END,
    spotify_stream_count,
    CASE
        WHEN spotify_stream_count IS NOT NULL
        THEN strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
        ELSE NULL
    END,
    CASE WHEN spotify_stream_count IS NOT NULL THEN 'spotify_web_hydration' ELSE NULL END,
    CASE
        WHEN spotify_stream_count IS NOT NULL THEN 'complete'
        WHEN spotify_url IS NULL THEN 'missing_link'
        ELSE 'pending'
    END,
    musicbrainz_id,
    apple_track_id,
    preview_url,
    artwork_url,
    apple_music_url,
    spotify_url,
    enabled
FROM songs;

DROP TABLE songs;
ALTER TABLE songs_with_spotify_popularity RENAME TO songs;

CREATE INDEX idx_songs_eligibility
    ON songs(enabled, release_year, popularity_score);
CREATE INDEX idx_songs_search
    ON songs(title COLLATE NOCASE, artist COLLATE NOCASE);
CREATE INDEX idx_songs_stream_count
    ON songs(enabled, stream_count DESC);

PRAGMA foreign_keys = ON;
