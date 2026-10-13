"""Compact catalog snapshot, rebuilt after each application catalog import."""

from __future__ import annotations

import json
from collections import OrderedDict
from collections.abc import Iterator, Sequence
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


class EligibleSongs(Sequence[int]):
    """A uniformly indexable song pool backed by a compact integer bitmap."""

    def __init__(self, ids: tuple[int, ...], positions: dict[int, int], mask: int):
        self.ids = ids
        self.positions = positions
        self.mask = mask

    def __len__(self) -> int:
        return self.mask.bit_count()

    def __getitem__(self, index: int) -> int:
        count = len(self)
        if index < 0:
            index += count
        if not 0 <= index < count:
            raise IndexError(index)
        mask = self.mask
        position = 0
        # Binary partitioning finds the selected set bit in O(log(catalog size))
        # C integer operations, without materializing thousands of eligible IDs.
        for exponent in range((len(self.ids) - 1).bit_length() - 1, -1, -1):
            width = 1 << exponent
            lower = mask & ((1 << width) - 1)
            lower_count = lower.bit_count()
            if index < lower_count:
                mask = lower
            else:
                index -= lower_count
                mask >>= width
                position += width
        return self.ids[position]

    def __iter__(self) -> Iterator[int]:
        mask = self.mask
        while mask:
            lowest = mask & -mask
            yield self.ids[lowest.bit_length() - 1]
            mask ^= lowest

    def without(self, excluded: Sequence[int]) -> EligibleSongs:
        mask = self.mask
        for song_id in excluded:
            position = self.positions.get(song_id)
            if position is not None:
                mask &= ~(1 << position)
        return EligibleSongs(self.ids, self.positions, mask)


@dataclass
class CatalogSnapshot:
    version: str
    songs: dict[int, tuple[int, int]]
    genres: dict[int, str]
    countries: dict[int, str]
    artists: dict[int, str]
    song_genres: dict[int, list[int]]
    song_countries: dict[int, list[int]]
    song_artists: dict[int, list[int]]
    browse_count: int
    _facets: OrderedDict[tuple[object, ...], int] = field(default_factory=OrderedDict)
    _summaries: OrderedDict[tuple[object, ...], object] = field(default_factory=OrderedDict)

    _ids: tuple[int, ...] = field(init=False, repr=False)
    _positions: dict[int, int] = field(init=False, repr=False)
    _all: int = field(init=False, repr=False)
    _genre_masks: dict[str, int] = field(init=False, repr=False)
    _country_masks: dict[str, int] = field(init=False, repr=False)
    _artist_masks: dict[str, int] = field(init=False, repr=False)
    _year_masks: dict[int, int] = field(init=False, repr=False)
    _score_masks: dict[int, int] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._ids = tuple(sorted(self.songs))
        self._positions = {song_id: index for index, song_id in enumerate(self._ids)}
        self._all = (1 << len(self._ids)) - 1
        byte_count = (len(self._ids) + 7) // 8

        def relation_masks(relations, names):
            buffers: dict[str, bytearray] = {}
            for song_id, dimension_ids in relations.items():
                position = self._positions[song_id]
                byte_index, bit_index = divmod(position, 8)
                bit = 1 << bit_index
                for dimension_id in dimension_ids:
                    name = names[dimension_id]
                    buffer = buffers.get(name)
                    if buffer is None:
                        buffer = buffers[name] = bytearray(byte_count)
                    buffer[byte_index] |= bit
            return {name: int.from_bytes(buffer, "little") for name, buffer in buffers.items()}

        self._genre_masks = relation_masks(self.song_genres, self.genres)
        self._country_masks = relation_masks(self.song_countries, self.countries)
        self._artist_masks = relation_masks(self.song_artists, self.artists)
        years: dict[int, bytearray] = {}
        scores: dict[int, bytearray] = {}
        for position, song_id in enumerate(self._ids):
            byte_index, bit_index = divmod(position, 8)
            bit = 1 << bit_index
            year, score = self.songs[song_id]
            for value, buffers in ((year, years), (score, scores)):
                buffer = buffers.get(value)
                if buffer is None:
                    buffer = buffers[value] = bytearray(byte_count)
                buffer[byte_index] |= bit
        self._year_masks = {year: int.from_bytes(years[year], "little") for year in sorted(years)}
        self._score_masks = {
            score: int.from_bytes(scores[score], "little") for score in sorted(scores)
        }

    @classmethod
    def from_payload(cls, payload: str, version: str) -> CatalogSnapshot:
        data = json.loads(payload)

        def relations(name: str) -> dict[int, list[int]]:
            result: dict[int, list[int]] = {}
            for song_id, dimension_id in data[name]:
                result.setdefault(song_id, []).append(dimension_id)
            return result

        return cls(
            version=version,
            songs={song_id: (year, score) for song_id, year, score in data["songs"]},
            genres={key: value for key, value in data["genres"]},
            countries={key: value for key, value in data["countries"]},
            artists={key: value for key, value in data["artists"]},
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

    @staticmethod
    def _union(masks: dict[str, int], selected: tuple[str, ...]) -> int:
        result = 0
        for value in selected:
            result |= masks.get(value, 0)
        return result

    @staticmethod
    def _range(masks: dict[int, int], minimum: int | None, maximum: int | None) -> int:
        result = 0
        for value, mask in masks.items():
            if (minimum is None or value >= minimum) and (maximum is None or value <= maximum):
                result |= mask
        return result

    def _pool(self, request: FilterContextRequest | RoundRequest, stage: int) -> int:
        key = self._key(request, stage)
        if key in self._facets:
            self._facets.move_to_end(key)
            return self._facets[key]
        if stage:
            pool = self._pool(request, stage - 1)
        else:
            pool = self._artist_masks.get(request.artist_id, 0) if request.artist_id else self._all
        if stage == 1 and key[1]:
            pool &= self._union(self._genre_masks, key[1])
        elif stage == 2 and key[2]:
            pool &= self._union(self._country_masks, key[2])
        elif stage == 3 and (request.year_min is not None or request.year_max is not None):
            pool &= self._range(self._year_masks, request.year_min, request.year_max)
        elif stage == 4 and (
            request.popularity_min is not None or request.popularity_max is not None
        ):
            pool &= self._range(self._score_masks, request.popularity_min, request.popularity_max)
        self._facets[key] = pool
        # Every entry holds at most ceil(catalog_size / 8) bytes, independent of
        # how many songs match. Bound entries without rescanning cached pools.
        while len(self._facets) > 128:
            self._facets.popitem(last=False)
        return pool

    def _summary(self, request: FilterContextRequest, stage: int):
        key = (stage, *self._key(request, stage))
        if key in self._summaries:
            self._summaries.move_to_end(key)
            return self._summaries[key]
        pool = self._pool(request, stage)
        if stage == 0:
            result = sorted(
                (name for name, mask in self._genre_masks.items() if pool & mask), key=str.lower
            )
        elif stage == 1:
            result = sorted(name for name, mask in self._country_masks.items() if pool & mask)
        elif stage in (2, 3):
            buckets = self._year_masks if stage == 2 else self._score_masks
            values = [value for value, mask in buckets.items() if pool & mask]
            result = (values[0], values[-1]) if values else (None, None)
        else:
            result = pool.bit_count()
        self._summaries[key] = result
        while len(self._summaries) > 128:
            self._summaries.popitem(last=False)
        return result

    def eligible_pool(self, request: RoundRequest, exclude: bool = False) -> EligibleSongs:
        pool = EligibleSongs(self._ids, self._positions, self._pool(request, 4))
        return pool.without(request.exclude_ids) if exclude else pool

    def eligible_ids(self, request: RoundRequest, exclude: bool = False) -> list[int]:
        # Compatibility for offline callers. Worker round selection uses the
        # indexable bitmap directly rather than allocating all matching IDs.
        return list(self.eligible_pool(request, exclude))

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
