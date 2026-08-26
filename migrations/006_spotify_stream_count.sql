ALTER TABLE songs ADD COLUMN spotify_stream_count INTEGER
    CHECK (spotify_stream_count IS NULL OR spotify_stream_count >= 0);
