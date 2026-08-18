from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Score = Annotated[int, Field(ge=0, le=100)]
Year = Annotated[int, Field(ge=1800, le=2200)]


class RoundRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    genres: list[str] = Field(default_factory=list, max_length=30)
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


class RoundResponse(BaseModel):
    song_id: int
    preview_url: str


class SongSearchResult(BaseModel):
    id: int
    title: str
    artist: str
    artwork_url: str | None


class SongReveal(BaseModel):
    id: int
    title: str
    artist: str
    album: str | None
    release_year: int
    artwork_url: str | None
    genres: list[str]
    preview_url: str


class FilterMetadata(BaseModel):
    genres: list[str]
    year_min: int | None
    year_max: int | None
    popularity_min: int
    popularity_max: int
    song_count: int


class ErrorDetail(BaseModel):
    code: Literal["NO_MATCHING_SONGS", "SONG_NOT_FOUND"]
    message: str
