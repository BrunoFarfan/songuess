PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS artists (
    id INTEGER PRIMARY KEY,
    musicbrainz_id TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    sort_name TEXT,
    disambiguation TEXT
);

CREATE TABLE IF NOT EXISTS song_artists (
    song_id INTEGER NOT NULL REFERENCES songs(id) ON DELETE CASCADE,
    artist_id INTEGER NOT NULL REFERENCES artists(id) ON DELETE CASCADE,
    credit_order INTEGER NOT NULL,
    credited_name TEXT NOT NULL,
    join_phrase TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (song_id, credit_order),
    UNIQUE (song_id, artist_id)
);

CREATE INDEX IF NOT EXISTS idx_song_artists_artist_song
    ON song_artists(artist_id, song_id);
