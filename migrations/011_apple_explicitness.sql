ALTER TABLE songs
ADD COLUMN apple_explicitness TEXT NOT NULL DEFAULT 'unknown'
CHECK (apple_explicitness IN ('explicit', 'not_explicit', 'cleaned', 'unknown'));

ALTER TABLE songs
ADD COLUMN apple_explicitness_checked_at TEXT;

CREATE INDEX idx_songs_apple_explicitness
ON songs(enabled, apple_explicitness, apple_explicitness_checked_at);
