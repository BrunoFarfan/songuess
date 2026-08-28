CREATE TABLE IF NOT EXISTS song_search (
    song_id INTEGER PRIMARY KEY REFERENCES songs(id) ON DELETE CASCADE,
    normalized_title TEXT NOT NULL COLLATE NOCASE,
    normalized_title_compact TEXT NOT NULL COLLATE NOCASE,
    normalized_artist TEXT NOT NULL COLLATE NOCASE,
    normalized_artist_compact TEXT NOT NULL COLLATE NOCASE,
    normalized_album TEXT NOT NULL COLLATE NOCASE,
    normalized_album_compact TEXT NOT NULL COLLATE NOCASE,
    normalized_year TEXT NOT NULL,
    normalized_text TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_song_search_title
    ON song_search(normalized_title, song_id);
CREATE INDEX IF NOT EXISTS idx_song_search_title_compact
    ON song_search(normalized_title_compact, song_id);
CREATE INDEX IF NOT EXISTS idx_song_search_artist_compact
    ON song_search(normalized_artist_compact, song_id);
CREATE INDEX IF NOT EXISTS idx_song_search_album_compact
    ON song_search(normalized_album_compact, song_id);

CREATE VIRTUAL TABLE IF NOT EXISTS song_search_fts USING fts5(
    song_id UNINDEXED,
    normalized_text,
    tokenize='trigram'
);

CREATE TABLE IF NOT EXISTS artist_search_aliases (
    artist_id INTEGER NOT NULL REFERENCES artists(id) ON DELETE CASCADE,
    alias TEXT NOT NULL,
    normalized_alias TEXT NOT NULL COLLATE NOCASE,
    normalized_alias_compact TEXT NOT NULL COLLATE NOCASE,
    PRIMARY KEY (artist_id, alias)
);

CREATE INDEX IF NOT EXISTS idx_artist_search_alias_compact
    ON artist_search_aliases(normalized_alias_compact, artist_id);

CREATE VIRTUAL TABLE IF NOT EXISTS artist_search_fts USING fts5(
    artist_id UNINDEXED,
    normalized_alias,
    tokenize='trigram'
);
