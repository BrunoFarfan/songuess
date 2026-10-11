"""Compact catalog snapshot, rebuilt after each application catalog import."""

from __future__ import annotations

import json
from collections import OrderedDict
from dataclasses import dataclass, field

from app.models import FilterContextRequest, FilterMetadata, RoundRequest

SNAPSHOT_REFRESH_SQL = """
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
"""


@dataclass
class CatalogSnapshot:
    version: str
    songs: dict[int, tuple[int, int]]
    genres: dict[int, str]
    countries: dict[int, str]
    artists: dict[int, str]
    song_genres: dict[int, set[int]]
    song_countries: dict[int, set[int]]
    song_artists: dict[int, set[int]]
    browse_count: int
    _facets: OrderedDict[tuple[object, ...], frozenset[int]] = field(default_factory=OrderedDict)
    _summaries: OrderedDict[tuple[object, ...], object] = field(default_factory=OrderedDict)

    @classmethod
    def from_payload(cls, payload: str, version: str) -> CatalogSnapshot:
        data = json.loads(payload)

        def relations(name: str) -> dict[int, set[int]]:
            result: dict[int, set[int]] = {}
            for song_id, dimension_id in data[name]:
                result.setdefault(int(song_id), set()).add(int(dimension_id))
            return result

        return cls(
            version=version,
            songs={int(song_id): (int(year), int(score)) for song_id, year, score in data["songs"]},
            genres={int(key): str(value) for key, value in data["genres"]},
            countries={int(key): str(value) for key, value in data["countries"]},
            artists={int(key): str(value) for key, value in data["artists"]},
            song_genres=relations("song_genres"),
            song_countries=relations("song_countries"),
            song_artists=relations("song_artists"),
            browse_count=int(data["browse_count"]),
        )

    @staticmethod
    def _key(request: FilterContextRequest | RoundRequest, stage: int) -> tuple[object, ...]:
        genres = tuple(sorted({g.strip().lower() for g in request.genres if g.strip()}))
        countries = tuple(sorted({c.strip().upper() for c in request.countries if c.strip()}))
        key: tuple[object, ...] = (request.artist_id,)
        if stage >= 1:
            key += (genres,)
        if stage >= 2:
            key += (countries,)
        if stage >= 3:
            key += (request.year_min, request.year_max)
        if stage >= 4:
            key += (request.popularity_min, request.popularity_max)
        return key

    def _pool(self, request: FilterContextRequest | RoundRequest, stage: int) -> frozenset[int]:
        key = self._key(request, stage)
        genres = set(key[1]) if stage >= 1 else set()
        countries = set(key[2]) if stage >= 2 else set()
        if key in self._facets:
            self._facets.move_to_end(key)
            return self._facets[key]
        if stage:
            previous = self._pool(request, stage - 1)
            pool = set(previous)
        else:
            pool = set(self.songs)
            if request.artist_id:
                artist_ids = {a for a, value in self.artists.items() if value == request.artist_id}
                pool.intersection_update(
                    s for s, ids in self.song_artists.items() if ids.intersection(artist_ids)
                )
        if stage == 1 and genres:
            genre_ids = {g for g, name in self.genres.items() if name in genres}
            pool.intersection_update(
                s for s, ids in self.song_genres.items() if ids.intersection(genre_ids)
            )
        if stage == 2 and countries:
            country_ids = {c for c, code in self.countries.items() if code in countries}
            pool.intersection_update(
                s for s, ids in self.song_countries.items() if ids.intersection(country_ids)
            )
        if stage == 3:
            pool = {
                s
                for s in pool
                if (request.year_min is None or self.songs[s][0] >= request.year_min)
                and (request.year_max is None or self.songs[s][0] <= request.year_max)
            }
        if stage == 4:
            pool = {
                s
                for s in pool
                if (request.popularity_min is None or self.songs[s][1] >= request.popularity_min)
                and (request.popularity_max is None or self.songs[s][1] <= request.popularity_max)
            }
        result = frozenset(pool)
        if stage and result == previous:
            result = previous
        self._facets[key] = result
        while (
            len(self._facets) > 128
            or sum(
                len(pool) for pool in {id(value): value for value in self._facets.values()}.values()
            )
            > 50_000
        ):
            self._facets.popitem(last=False)
        return result

    def _summary(self, request: FilterContextRequest, stage: int):
        key = (stage, *self._key(request, stage))
        if key in self._summaries:
            self._summaries.move_to_end(key)
            return self._summaries[key]
        pool = self._pool(request, stage)
        if stage == 0:
            result = sorted(
                {self.genres[g] for s in pool for g in self.song_genres.get(s, ())}, key=str.lower
            )
        elif stage == 1:
            result = sorted(
                {self.countries[c] for s in pool for c in self.song_countries.get(s, ())}
            )
        elif stage in (2, 3):
            values = [self.songs[s][stage - 2] for s in pool]
            result = (min(values, default=None), max(values, default=None))
        else:
            result = len(pool)
        self._summaries[key] = result
        while len(self._summaries) > 128:
            self._summaries.popitem(last=False)
        return result

    def eligible_ids(self, request: RoundRequest, exclude: bool = False) -> list[int]:
        pool = self._pool(request, 4)
        if exclude:
            pool = pool.difference(request.exclude_ids)
        return sorted(pool)

    def filter_metadata(self, request: FilterContextRequest | None = None) -> FilterMetadata:
        context = request or FilterContextRequest()
        year_min, year_max = self._summary(context, 2)
        score_min, score_max = self._summary(context, 3)
        return FilterMetadata(
            genres=self._summary(context, 0),
            countries=self._summary(context, 1),
            year_min=year_min,
            year_max=year_max,
            popularity_min=(score_min or 0) if request is not None else 0,
            popularity_max=(score_max or 0) if request is not None else 100,
            song_count=self._summary(context, 4),
        )
