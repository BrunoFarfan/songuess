from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Score = Annotated[int, Field(ge=0, le=100)]
Year = Annotated[int, Field(ge=1800, le=2200)]
CountryCode = Annotated[str, Field(min_length=2, max_length=2, pattern=r"^[A-Za-z]{2}$")]
ArtistId = Annotated[str, Field(min_length=1, max_length=64)]


class RoundRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    genres: list[str] = Field(default_factory=list, max_length=30)
    countries: list[CountryCode] = Field(default_factory=list, max_length=30)
    artist_id: ArtistId | None = None
    year_min: Year
    year_max: Year
    popularity_min: Score
    popularity_max: Score
    exclude_ids: list[int] = Field(default_factory=list, max_length=500)

    @model_validator(mode="after")
    def validate_ranges(self) -> "RoundRequest":
        if self.year_min > self.year_max:
            raise ValueError("year_min must be less than or equal to year_max")
        if self.popularity_min > self.popularity_max:
            raise ValueError("popularity_min must be less than or equal to popularity_max")
        return self


class FilterContextRequest(BaseModel):
    """Current wizard selections used to derive each following facet."""

    model_config = ConfigDict(extra="forbid")

    genres: list[str] = Field(default_factory=list, max_length=30)
    countries: list[CountryCode] = Field(default_factory=list, max_length=30)
    artist_id: ArtistId | None = None
    year_min: Year | None = None
    year_max: Year | None = None
    popularity_min: Score | None = None
    popularity_max: Score | None = None

    @model_validator(mode="after")
    def validate_optional_ranges(self) -> "FilterContextRequest":
        if (
            self.year_min is not None
            and self.year_max is not None
            and self.year_min > self.year_max
        ):
            raise ValueError("year_min must be less than or equal to year_max")
        if (
            self.popularity_min is not None
            and self.popularity_max is not None
            and self.popularity_min > self.popularity_max
        ):
            raise ValueError("popularity_min must be less than or equal to popularity_max")
        return self


class RoundResponse(BaseModel):
    song_id: int
    preview_url: str


class SongSearchResult(BaseModel):
    id: int
    title: str
    artist: str
    album: str | None
    release_year: int
    artwork_url: str | None
    popularity_score: Score | None


class SongSearchPage(BaseModel):
    items: list[SongSearchResult]
    offset: int
    limit: int
    total: int
    has_more: bool


class ArtistOption(BaseModel):
    id: str
    name: str
    disambiguation: str | None
    song_count: int


class SongReveal(BaseModel):
    id: int
    title: str
    artist: str
    album: str | None
    release_year: int
    artwork_url: str | None
    popularity_score: Score | None
    genres: list[str]
    preview_url: str
    apple_music_url: str | None
    spotify_url: str | None


class FilterMetadata(BaseModel):
    genres: list[str]
    countries: list[str]
    year_min: int | None
    year_max: int | None
    popularity_min: int
    popularity_max: int
    song_count: int


class ErrorDetail(BaseModel):
    code: Literal["NO_MATCHING_SONGS", "SONG_NOT_FOUND"]
    message: str
