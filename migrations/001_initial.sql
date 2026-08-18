PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS songs (
    id INTEGER PRIMARY KEY,
    title TEXT NOT NULL CHECK (length(trim(title)) > 0),
    artist TEXT NOT NULL CHECK (length(trim(artist)) > 0),
    album TEXT,
    release_year INTEGER NOT NULL CHECK (release_year BETWEEN 1800 AND 2200),
    popularity_score INTEGER NOT NULL CHECK (popularity_score BETWEEN 0 AND 100),
    listener_count INTEGER CHECK (listener_count IS NULL OR listener_count >= 0),
    listen_count INTEGER CHECK (listen_count IS NULL OR listen_count >= 0),
    musicbrainz_id TEXT UNIQUE,
    apple_track_id TEXT UNIQUE,
    preview_url TEXT NOT NULL CHECK (length(trim(preview_url)) > 0),
    artwork_url TEXT,
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1))
);

CREATE TABLE IF NOT EXISTS genres (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL COLLATE NOCASE UNIQUE CHECK (
        length(trim(name)) > 0 AND name = lower(trim(name))
    )
);

CREATE TABLE IF NOT EXISTS song_genres (
    song_id INTEGER NOT NULL REFERENCES songs(id) ON DELETE CASCADE,
    genre_id INTEGER NOT NULL REFERENCES genres(id) ON DELETE CASCADE,
    PRIMARY KEY (song_id, genre_id)
);

CREATE INDEX IF NOT EXISTS idx_songs_eligibility
    ON songs(enabled, release_year, popularity_score);
CREATE INDEX IF NOT EXISTS idx_song_genres_genre_song
    ON song_genres(genre_id, song_id);
CREATE INDEX IF NOT EXISTS idx_songs_search
    ON songs(title COLLATE NOCASE, artist COLLATE NOCASE);

