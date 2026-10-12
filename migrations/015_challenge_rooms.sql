CREATE TABLE rooms (
    code TEXT PRIMARY KEY CHECK(length(code) = 8),
    creator_guest_id TEXT NOT NULL,
    filters_json TEXT NOT NULL,
    song_count INTEGER NOT NULL CHECK(song_count BETWEEN 1 AND 20),
    created_at INTEGER NOT NULL,
    closes_at INTEGER NOT NULL,
    delete_at INTEGER NOT NULL CHECK(delete_at > closes_at)
);
CREATE INDEX idx_rooms_delete_at ON rooms(delete_at);
CREATE TABLE room_songs (
    room_code TEXT NOT NULL REFERENCES rooms(code) ON DELETE CASCADE,
    position INTEGER NOT NULL CHECK(position BETWEEN 1 AND 20),
    answer_apple_track_id TEXT NOT NULL,
    preview_url TEXT NOT NULL,
    reveal_json TEXT NOT NULL,
    PRIMARY KEY(room_code, position),
    UNIQUE(room_code, answer_apple_track_id)
);
CREATE TABLE participants (
    room_code TEXT NOT NULL REFERENCES rooms(code) ON DELETE CASCADE,
    guest_id TEXT NOT NULL,
    display_name TEXT NOT NULL CHECK(length(trim(display_name)) BETWEEN 1 AND 24),
    joined_at INTEGER NOT NULL,
    PRIMARY KEY(room_code, guest_id)
);
CREATE TABLE participant_rounds (
    room_code TEXT NOT NULL,
    guest_id TEXT NOT NULL,
    position INTEGER NOT NULL,
    clue_number INTEGER NOT NULL DEFAULT 1 CHECK(clue_number BETWEEN 1 AND 6),
    guesses_json TEXT NOT NULL DEFAULT '[]',
    result TEXT CHECK(result IN ('clue_1','clue_2','clue_3','clue_4','clue_5','clue_6','not_guessed')),
    version INTEGER NOT NULL DEFAULT 0 CHECK(version >= 0),
    completed_at INTEGER,
    PRIMARY KEY(room_code, guest_id, position),
    FOREIGN KEY(room_code, guest_id) REFERENCES participants(room_code, guest_id) ON DELETE CASCADE,
    FOREIGN KEY(room_code, position) REFERENCES room_songs(room_code, position) ON DELETE CASCADE
);
