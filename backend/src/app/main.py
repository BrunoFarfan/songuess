import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Query

from app.database import Database, initialize_database, request_database
from app.models import (
    ArtistOption,
    FilterContextRequest,
    FilterMetadata,
    RoundRequest,
    RoundResponse,
    SongReveal,
    SongSearchPage,
)
from app.repository import (
    choose_round_async,
    get_contextual_filter_metadata_async,
    get_filter_metadata_async,
    get_song_async,
    search_artists_async,
    search_songs_async,
)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    if sys.platform != "emscripten":
        initialize_database()
    yield


app = FastAPI(
    title="Songuess API",
    version="0.1.0",
    lifespan=lifespan,
)


@app.get("/api/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post(
    "/api/round",
    response_model=RoundResponse,
    responses={404: {"description": "No songs match the requested filters"}},
)
async def create_round(
    request: RoundRequest,
    database: Annotated[Database, Depends(request_database)],
) -> RoundResponse:
    round_response = await choose_round_async(database, request)
    if round_response is None:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "NO_MATCHING_SONGS",
                "message": "No songs match these filters yet.",
            },
        )
    return round_response


@app.get("/api/songs/search", response_model=SongSearchPage)
async def songs_search(
    database: Annotated[Database, Depends(request_database)],
    q: str = Query(default="", max_length=120),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=40, ge=1, le=100),
) -> SongSearchPage:
    items, total = await search_songs_async(database, q, limit=limit, offset=offset)
    return SongSearchPage(
        items=items,
        offset=offset,
        limit=limit,
        total=total,
        has_more=offset + len(items) < total,
    )


@app.get("/api/artists/search", response_model=list[ArtistOption])
async def artists_search(
    database: Annotated[Database, Depends(request_database)],
    q: str = Query(min_length=1, max_length=120),
) -> list[ArtistOption]:
    return await search_artists_async(database, q)


@app.get(
    "/api/songs/{song_id}",
    response_model=SongReveal,
    responses={404: {"description": "Song not found"}},
)
async def song_reveal(
    song_id: int,
    database: Annotated[Database, Depends(request_database)],
) -> SongReveal:
    song = await get_song_async(database, song_id)
    if song is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "SONG_NOT_FOUND", "message": "This song is unavailable."},
        )
    return song


@app.get("/api/filters", response_model=FilterMetadata)
async def filters(
    database: Annotated[Database, Depends(request_database)],
) -> FilterMetadata:
    return await get_filter_metadata_async(database)


@app.post("/api/filters/context", response_model=FilterMetadata)
async def contextual_filters(
    request: FilterContextRequest,
    database: Annotated[Database, Depends(request_database)],
) -> FilterMetadata:
    return await get_contextual_filter_metadata_async(database, request)
