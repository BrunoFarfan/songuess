from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query

from app.database import database_connection, initialize_database
from app.models import (
    FilterMetadata,
    RoundRequest,
    RoundResponse,
    SongReveal,
    SongSearchResult,
)
from app.repository import choose_round, get_filter_metadata, get_song, search_songs


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    initialize_database()
    yield


app = FastAPI(
    title="Songuess API",
    version="0.1.0",
    lifespan=lifespan,
)


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post(
    "/api/round",
    response_model=RoundResponse,
    responses={404: {"description": "No songs match the requested filters"}},
)
def create_round(request: RoundRequest) -> RoundResponse:
    with database_connection() as connection:
        round_response = choose_round(connection, request)
    if round_response is None:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "NO_MATCHING_SONGS",
                "message": "No songs match these filters yet.",
            },
        )
    return round_response


@app.get("/api/songs/search", response_model=list[SongSearchResult])
def songs_search(q: str = Query(min_length=2, max_length=120)) -> list[SongSearchResult]:
    with database_connection() as connection:
        return search_songs(connection, q)


@app.get(
    "/api/songs/{song_id}",
    response_model=SongReveal,
    responses={404: {"description": "Song not found"}},
)
def song_reveal(song_id: int) -> SongReveal:
    with database_connection() as connection:
        song = get_song(connection, song_id)
    if song is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "SONG_NOT_FOUND", "message": "This song is unavailable."},
        )
    return song


@app.get("/api/filters", response_model=FilterMetadata)
def filters() -> FilterMetadata:
    with database_connection() as connection:
        return get_filter_metadata(connection)
