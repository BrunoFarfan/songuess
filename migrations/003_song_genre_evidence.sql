PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS song_genre_evidence (
    song_id INTEGER NOT NULL,
    genre_id INTEGER NOT NULL,
    rank INTEGER NOT NULL CHECK (rank BETWEEN 1 AND 3),
    score INTEGER NOT NULL CHECK (score >= 0),
    source TEXT NOT NULL CHECK (source IN ('apple', 'musicbrainz', 'fallback')),
    PRIMARY KEY (song_id, genre_id),
    UNIQUE (song_id, rank),
    FOREIGN KEY (song_id, genre_id)
        REFERENCES song_genres(song_id, genre_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_song_genre_evidence_song_rank
    ON song_genre_evidence(song_id, rank);
