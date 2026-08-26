from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass
from typing import Any

MIN_MUSICBRAINZ_VOTES = 3
MIN_RELATIVE_MUSICBRAINZ_SCORE = 0.40
MAX_GENRES_PER_SONG = 3


@dataclass(frozen=True)
class GenreClassification:
    name: str
    source: str
    score: int


_APPLE_GENRES = {
    "alternative": "alternative",
    "ambient": "electronic",
    "blues rock": "rock",
    "breakbeat": "dance",
    "contemporary folk": "folk",
    "country": "country",
    "christmas classic": "classical",
    "christmas pop": "pop",
    "dance": "dance",
    "downtempo": "electronic",
    "electronic": "electronic",
    "electronica": "electronic",
    "folk rock": "folk",
    "folk": "folk",
    "hard rock": "rock",
    "hip hop rap": "hip-hop",
    "hip hop": "hip-hop",
    "house": "dance",
    "indie pop": "pop",
    "indie rock": "alternative",
    "jazz": "jazz",
    "j pop": "pop",
    "k pop": "k-pop",
    "latin": "latin",
    "metal": "metal",
    "original score": "soundtrack",
    "alternative rap": "hip-hop",
    "pop": "pop",
    "pop rock": "rock",
    "prog rock art rock": "rock",
    "punk": "punk",
    "r b soul": "r&b",
    "rap": "hip-hop",
    "reggae": "reggae",
    "roots reggae": "reggae",
    "rock": "rock",
    "singer songwriter": "folk",
    "soundtrack": "soundtrack",
    "techno": "dance",
    "trance": "dance",
    "vocal pop": "pop",
    "korean hip hop": "hip-hop",
    "tv soundtrack": "soundtrack",
}

_MUSICBRAINZ_ALIASES = {
    "alternative": {
        "alternative",
        "alternative and punk",
        "alternative indie rock",
        "alternative pop",
        "alternative pop rock",
        "alternative punk",
        "alternative rock",
        "alternative dance",
        "britpop",
        "dream pop",
        "emo",
        "grunge",
        "indie",
        "indie pop",
        "indie rock",
        "indietronica",
        "neo psychedelia",
        "new wave",
        "post grunge",
        "post punk",
        "post punk revival",
        "shoegaze",
    },
    "classical": {
        "baroque",
        "classical",
        "contemporary classical",
        "modern classical",
        "opera",
        "orchestral",
    },
    "country": {
        "alternative country",
        "contemporary country",
        "country",
        "country pop",
        "country rock",
    },
    "dance": {
        "alternative dance",
        "breakbeat",
        "club dance",
        "dance",
        "dance pop",
        "disco",
        "electro house",
        "house",
        "progressive house",
        "techno",
        "trance",
    },
    "electronic": {
        "ambient",
        "big beat",
        "breakbeat",
        "downtempo",
        "electro",
        "electronic",
        "electronica",
        "electropop",
        "elektro",
        "idm",
        "indietronica",
        "leftfield",
        "synth pop",
        "synthpop",
        "trip hop",
        "trance",
    },
    "folk": {
        "alternative folk",
        "anti folk",
        "contemporary folk",
        "folk",
        "folk pop",
        "folk rock",
        "singer songwriter",
    },
    "hip-hop": {
        "abstract hip hop",
        "alternative hip hop",
        "boom bap",
        "hip hop",
        "hip hop rap",
        "hip-hop",
        "pop rap",
        "rap",
        "rap rock",
        "rap metal",
        "trip hop",
    },
    "jazz": {
        "acid jazz",
        "contemporary jazz",
        "jazz",
        "jazz fusion",
        "soul jazz",
    },
    "k-pop": {"k pop", "k-pop", "kpop"},
    "latin": {
        "bachata",
        "latin",
        "latin pop",
        "reggaeton",
        "salsa",
    },
    "metal": {
        "alternative metal",
        "death metal",
        "doom metal",
        "heavy metal",
        "industrial metal",
        "metal",
        "metalcore",
        "nu metal",
        "progressive metal",
        "rap metal",
        "speed metal",
        "thrash metal",
    },
    "pop": {
        "adult contemporary",
        "alternative pop",
        "art pop",
        "ambient pop",
        "baroque pop",
        "chamber pop",
        "dance pop",
        "dream pop",
        "electropop",
        "europop",
        "indie pop",
        "pop",
        "pop music",
        "pop punk",
        "pop rap",
        "pop rock",
        "pop soul",
        "power pop",
        "synth pop",
        "synthpop",
        "teen pop",
    },
    "punk": {
        "alternative and punk",
        "alternative punk",
        "emo",
        "hardcore punk",
        "pop punk",
        "post hardcore",
        "post punk",
        "post punk revival",
        "punk",
        "punk pop",
        "punk rock",
        "punk revival",
    },
    "r&b": {
        "blue eyed soul",
        "contemporary r b",
        "funk",
        "neo soul",
        "pop soul",
        "r b",
        "rhythm and blues",
        "soul",
    },
    "reggae": {"dub", "reggae", "roots reggae", "ska"},
    "rock": {
        "album rock",
        "alternative indie rock",
        "alternative pop rock",
        "alternative rock",
        "arena rock",
        "art rock",
        "blues rock",
        "acoustic rock",
        "alt rock",
        "classic rock",
        "contemporary pop rock",
        "country rock",
        "experimental rock",
        "folk rock",
        "funk rock",
        "gothic rock",
        "garage rock",
        "garage rock revival",
        "hard rock",
        "indie rock",
        "industrial rock",
        "pop rock",
        "post rock",
        "progressive rock",
        "psychedelic rock",
        "punk rock",
        "rap rock",
        "rock",
        "rock and roll",
        "rock music",
        "rock pop",
        "soft rock",
        "southern rock",
        "stoner rock",
    },
    "soundtrack": {"film score", "film scores", "original score", "soundtrack"},
}


def normalize_genre_label(value: object) -> str:
    ascii_value = (
        unicodedata.normalize("NFKD", str(value or ""))
        .encode("ascii", "ignore")
        .decode("ascii")
        .casefold()
    )
    return " ".join(re.findall(r"[a-z0-9]+", ascii_value))


def apple_genre(value: object) -> str | None:
    return _APPLE_GENRES.get(normalize_genre_label(value))


def musicbrainz_tag_genres(value: object) -> tuple[str, ...]:
    normalized = normalize_genre_label(value)
    return tuple(genre for genre, aliases in _MUSICBRAINZ_ALIASES.items() if normalized in aliases)


def classify_genres(
    apple_track: dict[str, Any], metadata: dict[str, Any]
) -> list[GenreClassification]:
    primary = apple_genre(apple_track.get("primaryGenreName"))
    scores: dict[str, int] = {}
    for tag in metadata.get("tags", []):
        if not isinstance(tag, dict):
            continue
        try:
            votes = int(tag.get("count") or 0)
        except (TypeError, ValueError):
            continue
        if votes <= 0:
            continue
        for genre in musicbrainz_tag_genres(tag.get("name")):
            scores[genre] = max(scores.get(genre, 0), votes)

    strongest_score = max(scores.values(), default=0)
    if primary:
        scores.pop(primary, None)

    supported: list[tuple[str, int]] = []
    if scores:
        minimum_score = max(
            MIN_MUSICBRAINZ_VOTES,
            math.ceil(strongest_score * MIN_RELATIVE_MUSICBRAINZ_SCORE),
        )
        supported = sorted(
            ((genre, score) for genre, score in scores.items() if score >= minimum_score),
            key=lambda item: (-item[1], item[0]),
        )

    classifications: list[GenreClassification] = []
    if primary:
        classifications.append(GenreClassification(primary, "apple", 100))
    for genre, score in supported[: MAX_GENRES_PER_SONG - len(classifications)]:
        classifications.append(GenreClassification(genre, "musicbrainz", score))
    if not classifications:
        classifications.append(GenreClassification("other", "fallback", 0))
    return classifications


def canonical_genres(apple_track: dict[str, Any], metadata: dict[str, Any]) -> list[str]:
    return [classification.name for classification in classify_genres(apple_track, metadata)]
