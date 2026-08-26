from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest
from dataset.genres import canonical_genres, classify_genres
from dataset.populate import audit_catalog_genres, initialize_database, write_catalog

from app.database import connect
from app.repository import get_song


def _apple(primary_genre: str, *, track_id: int = 1) -> dict[str, Any]:
    return {
        "trackId": track_id,
        "trackName": "Song",
        "artistName": "Artist",
        "collectionName": "Album",
        "canonicalReleaseYear": 2000,
        "previewUrl": "https://audio.example/1.m4a",
        "primaryGenreName": primary_genre,
    }


def _metadata(*tags: tuple[str, int]) -> dict[str, Any]:
    return {"tags": [{"name": name, "count": votes} for name, votes in tags]}


def _match(primary_genre: str = "Pop") -> dict[str, Any]:
    return {
        "candidate": {"recording_mbid": "recording", "listen_count": 100},
        "apple": _apple(primary_genre),
        "rank": 1,
    }


def test_genre_classifier_ignores_zero_negative_and_incidental_compound_tags() -> None:
    apple = _apple("Rock")
    metadata = _metadata(
        ("new wave", 15),
        ("alternative rock", 6),
        ("pop", 5),
        ("electronic", 0),
        ("country", -1),
        ("electronic/ambient/rock/jazz", 99),
    )

    classifications = classify_genres(apple, metadata)

    assert [(item.name, item.source, item.score) for item in classifications] == [
        ("rock", "apple", 100),
        ("alternative", "musicbrainz", 15),
    ]


def test_genre_classifier_requires_consensus_and_caps_ranked_supplements() -> None:
    apple = _apple("Rock")
    metadata = _metadata(
        ("pop punk", 8),
        ("alternative rock", 7),
        ("electronic", 3),
        ("country", 1),
    )

    assert canonical_genres(apple, metadata) == ["rock", "pop", "punk"]


def test_primary_genre_consensus_prevents_weak_supplements_from_being_promoted() -> None:
    apple = _apple("Pop")
    metadata = _metadata(("pop", 20), ("jazz", 3))

    assert canonical_genres(apple, metadata) == ["pop"]


def test_genre_classifier_uses_fallback_when_no_supported_source_exists() -> None:
    assert canonical_genres(_apple("Holiday"), _metadata(("mood: happy", 20), ("jazz", 1))) == [
        "other"
    ]


def test_genre_audit_is_dry_run_then_rebuilds_ranked_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "catalog.sqlite3"
    cache = tmp_path / "cache"
    initialize_database(database)
    metadata = _metadata(
        ("alternative", 10),
        ("pop", 5),
        ("punk", -1),
        ("electronic", 0),
    )
    write_catalog(database, [_match()], {"recording": metadata})

    with sqlite3.connect(database) as connection:
        connection.execute("DELETE FROM song_genres")
        for genre in ("electronic", "pop", "punk"):
            connection.execute("INSERT OR IGNORE INTO genres (name) VALUES (?)", (genre,))
            genre_id = connection.execute(
                "SELECT id FROM genres WHERE name = ?", (genre,)
            ).fetchone()[0]
            connection.execute(
                "INSERT INTO song_genres (song_id, genre_id) VALUES (1, ?)", (genre_id,)
            )

    monkeypatch.setattr(
        "dataset.populate.fetch_musicbrainz_metadata",
        lambda cache_dir, candidates: {"recording": metadata},
    )
    monkeypatch.setattr(
        "dataset.populate.read_cached_apple_track",
        lambda cache_dir, recording_mbid, country="US": _apple("Pop"),
    )

    dry_run = audit_catalog_genres(database, cache, report_path=tmp_path / "dry-run.json")
    assert dry_run["applied"] is False
    assert dry_run["ignored_nonpositive_tag_count"] == 2
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM song_genres").fetchone()[0] == 3

    applied = audit_catalog_genres(
        database,
        cache,
        apply=True,
        report_path=tmp_path / "applied.json",
    )
    assert applied["applied"] is True
    with sqlite3.connect(database) as connection:
        rows = connection.execute(
            "SELECT g.name, e.rank, e.score, e.source FROM song_genres sg "
            "JOIN genres g ON g.id = sg.genre_id "
            "JOIN song_genre_evidence e "
            "ON e.song_id = sg.song_id AND e.genre_id = sg.genre_id ORDER BY e.rank"
        ).fetchall()
        connection.execute("UPDATE songs SET popularity_score = 55 WHERE id = 1")
    assert rows == [
        ("pop", 1, 100, "apple"),
        ("alternative", 2, 10, "musicbrainz"),
    ]
    with connect(database) as connection:
        revealed_song = get_song(connection, 1)
    assert revealed_song is not None
    assert revealed_song.genres == ["pop", "alternative"]
    assert revealed_song.popularity_score == 55
