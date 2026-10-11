CREATE TABLE IF NOT EXISTS catalog_runtime (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    version TEXT NOT NULL,
    payload TEXT NOT NULL
);

WITH eligible AS (
    SELECT id, release_year, popularity_score FROM songs
    WHERE enabled = 1 AND preview_url <> '' AND popularity_score IS NOT NULL
)
INSERT OR REPLACE INTO catalog_runtime (id, version, payload)
SELECT 1, lower(hex(randomblob(16))), json_object(
    'songs', (SELECT json_group_array(json_array(id, release_year, popularity_score))
        FROM (SELECT id, release_year, popularity_score FROM eligible
            ORDER BY id)),
    'genres', (SELECT json_group_array(json_array(id, name))
        FROM (SELECT id, name FROM genres
            WHERE id IN (SELECT genre_id FROM song_genres JOIN eligible ON eligible.id = song_id)
            ORDER BY id)),
    'countries', (SELECT json_group_array(json_array(id, code))
        FROM (SELECT id, code FROM countries
            WHERE id IN (SELECT country_id FROM song_countries
                JOIN eligible ON eligible.id = song_id)
            ORDER BY id)),
    'artists', (SELECT json_group_array(json_array(id, musicbrainz_id))
        FROM (SELECT id, musicbrainz_id FROM artists
            WHERE id IN (SELECT artist_id FROM song_artists JOIN eligible ON eligible.id = song_id)
            ORDER BY id)),
    'song_genres', (SELECT json_group_array(json_array(song_id, genre_id))
        FROM (SELECT song_id, genre_id FROM song_genres
            WHERE song_id IN (SELECT id FROM eligible)
            ORDER BY song_id, genre_id)),
    'song_countries', (SELECT json_group_array(json_array(song_id, country_id))
        FROM (SELECT song_id, country_id FROM song_countries
            WHERE song_id IN (SELECT id FROM eligible)
            ORDER BY song_id, country_id)),
    'song_artists', (SELECT json_group_array(json_array(song_id, artist_id))
        FROM (SELECT song_id, artist_id FROM song_artists
            WHERE song_id IN (SELECT id FROM eligible)
            ORDER BY song_id, artist_id)),
    'browse_count', (SELECT COUNT(*) FROM songs WHERE enabled = 1 AND preview_url <> '')
);
