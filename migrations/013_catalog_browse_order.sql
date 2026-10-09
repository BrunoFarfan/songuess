-- Browse directly in catalog order without sorting every eligible song per page.
CREATE INDEX IF NOT EXISTS idx_song_search_browse
    ON song_search(normalized_title, normalized_artist, song_id);
