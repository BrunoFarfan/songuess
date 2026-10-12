"""Anonymous, asynchronous challenges with frozen catalog answers."""

import hashlib
import hmac
import json
import os
import secrets
import time
from datetime import UTC, datetime
from typing import Annotated, Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.database import Database, request_database
from app.models import RoundRequest, SongReveal, SongSearchResult
from app.repository import _round_filter

router = APIRouter(prefix="/api")
COOKIE = "songuess_guest"
SESSION_AGE = 30 * 86400
RETENTION = 7 * 86400
DB = Annotated[Database, Depends(request_database)]


class RoomFilters(RoundRequest):
    # Public room filters deliberately have no caller-selected excluded songs.
    exclude_ids: list[int] = Field(default_factory=list, exclude=True, max_length=0)

    @model_validator(mode="before")
    @classmethod
    def forbid_exclusions(cls, value):
        if isinstance(value, dict) and "exclude_ids" in value:
            raise ValueError("Room filters do not accept exclude_ids")
        return value


class NamedRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    display_name: str = Field(min_length=1, max_length=24)

    @field_validator("display_name", mode="before")
    @classmethod
    def trim_name(cls, value):
        return value.strip() if isinstance(value, str) else value


class CreateRoom(NamedRequest):
    filters: RoomFilters
    song_count: int = Field(default=10, ge=1, le=20)
    duration_hours: int = Field(default=24, ge=1, le=24)


class RoomAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    position: int = Field(ge=1, le=20)
    expected_version: int = Field(ge=0)
    action: Literal["guess", "skip", "give_up"]
    song_id: int | None = Field(default=None, gt=0)


RoundResult = Literal["clue_1", "clue_2", "clue_3", "clue_4", "clue_5", "clue_6", "not_guessed"]


class LeaderboardEntry(BaseModel):
    display_name: str
    rank: int
    points: int
    correct_count: int
    completed_count: int
    is_you: bool


class RoomSummary(BaseModel):
    code: str
    filters: RoomFilters
    song_count: int
    created_at: str
    closes_at: str
    delete_at: str
    status: Literal["active", "closed"]
    participant_count: int
    is_member: bool
    leaderboard: list[LeaderboardEntry]


class CurrentRound(BaseModel):
    position: int
    preview_url: str
    clue_number: int
    guesses: list[SongSearchResult]
    version: int


class CompletedRound(BaseModel):
    position: int
    result: RoundResult
    completed_at: str
    reveal: SongReveal


class PlaylistSong(BaseModel):
    position: int
    reveal: SongReveal


class RoomState(BaseModel):
    code: str
    guest_id: str
    display_name: str
    closed: bool
    closes_at: str
    playlist: list[PlaylistSong] = Field(max_length=20)
    completed_count: int
    total_points: int
    current_round: CurrentRound | None
    results: list[CompletedRound]


class ActionRound(BaseModel):
    position: int
    clue_number: int
    guesses: list[SongSearchResult]
    version: int
    result: RoundResult | None
    reveal: SongReveal | None


class ActionResponse(BaseModel):
    state: RoomState
    round: ActionRound


def _timestamp(value: int | None) -> str | None:
    return (
        datetime.fromtimestamp(value, UTC).isoformat().replace("+00:00", "Z")
        if value is not None
        else None
    )


def now() -> int:
    return int(time.time())


def _secret(request: Request) -> bytes:
    environment = request.scope.get("env")
    if environment is not None:
        value = getattr(environment, "SONGUESS_GUEST_SECRET", None)
        if not value:
            raise HTTPException(503, "Guest sessions are temporarily unavailable")
    else:
        value = os.getenv("SONGUESS_GUEST_SECRET")
        if not value:
            if os.getenv("SONGUESS_ENV", "development") != "development":
                raise HTTPException(503, "Guest sessions are temporarily unavailable")
            value = "songuess-local-development-only-secret"
    if len(str(value)) < 32:
        raise HTTPException(503, "Guest sessions are temporarily unavailable")
    return str(value).encode()


def _guest(request: Request, required: bool = True) -> str | None:
    secret = _secret(request)
    token = request.cookies.get(COOKIE, "")
    try:
        guest, expiry, signature = token.split(".")
        payload = f"{guest}.{expiry}"
        expected = hmac.new(secret, payload.encode(), hashlib.sha256).hexdigest()
        if (
            len(guest) == 32
            and all(char in "0123456789abcdef" for char in guest)
            and int(expiry) > now()
            and hmac.compare_digest(signature, expected)
        ):
            return guest
    except (ValueError, TypeError):
        pass
    if required:
        raise HTTPException(401, "Start a guest session first")
    return None


def _origin(request: Request) -> None:
    origin = request.headers.get("origin")
    if request.headers.get("sec-fetch-site") == "cross-site":
        raise HTTPException(403, "Cross-site request rejected")
    if origin:
        try:
            parsed = urlsplit(origin)
        except ValueError:
            raise HTTPException(403, "Cross-site request rejected") from None
        if (parsed.scheme, parsed.netloc) != (request.url.scheme, request.url.netloc):
            raise HTTPException(403, "Cross-site request rejected")


@router.post("/guest-session")
async def guest_session(request: Request, response: Response):
    _origin(request)
    guest = _guest(request, required=False) or secrets.token_hex(16)
    payload = f"{guest}.{now() + SESSION_AGE}"
    signature = hmac.new(_secret(request), payload.encode(), hashlib.sha256).hexdigest()
    response.set_cookie(
        COOKIE,
        f"{payload}.{signature}",
        max_age=SESSION_AGE,
        httponly=True,
        secure=request.url.scheme == "https"
        or (
            request.scope.get("env") is not None
            and request.url.hostname not in {"localhost", "127.0.0.1", "::1"}
        ),
        samesite="lax",
        path="/",
    )
    return {"guest_id": guest}


async def _room(database: Database, code: str):
    row = await database.fetch_one(
        "SELECT * FROM rooms WHERE code = ? AND delete_at > ?", (code, now())
    )
    if row is None:
        raise HTTPException(404, "Challenge unavailable")
    return row


def _score(rows) -> tuple[int, ...]:
    counts = [sum(row["result"] == f"clue_{clue}" for row in rows) for clue in range(1, 7)]
    return (sum((6 - index) * count for index, count in enumerate(counts)), sum(counts), *counts)


async def _summary(database: Database, room, guest: str | None):
    participants = await database.fetch_all(
        "SELECT * FROM participants WHERE room_code = ? ORDER BY joined_at, guest_id",
        (room["code"],),
    )
    rounds = await database.fetch_all(
        "SELECT * FROM participant_rounds WHERE room_code = ?", (room["code"],)
    )
    ranked = []
    for participant in participants:
        own = [row for row in rounds if row["guest_id"] == participant["guest_id"]]
        ranked.append((_score(own), participant, sum(row["result"] is not None for row in own)))
    ranked.sort(key=lambda entry: entry[0], reverse=True)
    leaderboard = []
    previous = None
    rank = 0
    for index, (score, participant, completed) in enumerate(ranked, 1):
        if score != previous:
            rank = index
        previous = score
        leaderboard.append(
            {
                "display_name": participant["display_name"],
                "rank": rank,
                "points": score[0],
                "correct_count": score[1],
                "completed_count": completed,
                "is_you": participant["guest_id"] == guest,
            }
        )
    return {
        "code": room["code"],
        "filters": json.loads(room["filters_json"]),
        "song_count": room["song_count"],
        "created_at": _timestamp(room["created_at"]),
        "closes_at": _timestamp(room["closes_at"]),
        "delete_at": _timestamp(room["delete_at"]),
        "status": "closed" if now() >= room["closes_at"] else "active",
        "participant_count": len(participants),
        "is_member": any(item["is_you"] for item in leaderboard),
        "leaderboard": leaderboard,
    }


async def _state(database: Database, room, guest: str):
    member = await database.fetch_one(
        "SELECT * FROM participants WHERE room_code = ? AND guest_id = ?", (room["code"], guest)
    )
    if member is None:
        raise HTTPException(403, "Join this challenge first")
    rows = await database.fetch_all(
        "SELECT pr.*, rs.preview_url, rs.reveal_json FROM participant_rounds pr "
        "JOIN room_songs rs USING(room_code, position) WHERE pr.room_code = "
        "? AND pr.guest_id = ? ORDER BY position",
        (room["code"], guest),
    )
    closed = now() >= room["closes_at"]
    current = next((row for row in rows if row["result"] is None), None)
    results = [
        {
            "position": row["position"],
            "result": row["result"] or "not_guessed",
            "completed_at": _timestamp(
                row["completed_at"] if row["result"] is not None else room["closes_at"]
            ),
            "reveal": json.loads(row["reveal_json"]),
        }
        for row in rows
        if row["result"] is not None or closed
    ]
    return {
        "code": room["code"],
        "guest_id": guest,
        "display_name": member["display_name"],
        "closed": closed,
        "closes_at": _timestamp(room["closes_at"]),
        "playlist": [
            {"position": row["position"], "reveal": json.loads(row["reveal_json"])} for row in rows
        ],
        "completed_count": sum(row["result"] is not None for row in rows),
        "total_points": _score(rows)[0],
        "results": results,
        "current_round": None
        if closed or current is None
        else {
            "position": current["position"],
            "preview_url": current["preview_url"],
            "clue_number": current["clue_number"],
            "guesses": _public_guesses(json.loads(current["guesses_json"])),
            "version": current["version"],
        },
    }


@router.post("/rooms", response_model=RoomSummary)
async def create_room(body: CreateRoom, request: Request, database: DB):
    _origin(request)
    guest = _guest(request)
    clauses, parameters = _round_filter(body.filters)
    songs = await database.fetch_all(
        "SELECT s.*, (SELECT json_group_array(name) FROM (SELECT g.name FROM genres g "
        "JOIN song_genres sg ON sg.genre_id=g.id WHERE sg.song_id=s.id ORDER BY g.name)) "
        "AS genres_json FROM songs s WHERE "
        + " AND ".join(clauses)
        + " AND s.apple_track_id IS NOT NULL AND s.apple_track_id <> '' AND "
        + "s.preview_url <> '' ORDER BY random() LIMIT ?",
        (*parameters, body.song_count),
    )
    if len(songs) != body.song_count:
        raise HTTPException(422, "Not enough songs match these filters")
    code = "".join(secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(8))
    created = now()
    closes = created + body.duration_hours * 3600
    statements = [
        (
            "INSERT INTO rooms VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                code,
                guest,
                body.filters.model_dump_json(),
                body.song_count,
                created,
                closes,
                closes + RETENTION,
            ),
        ),
        ("INSERT INTO participants VALUES (?, ?, ?, ?)", (code, guest, body.display_name, created)),
    ]
    for position, song in enumerate(songs, 1):
        reveal = SongReveal.model_validate(
            {field: song[field] for field in SongReveal.model_fields if field != "genres"}
            | {"genres": json.loads(song["genres_json"])}
        )
        statements.extend(
            [
                (
                    "INSERT INTO room_songs VALUES (?, ?, ?, ?, ?)",
                    (
                        code,
                        position,
                        song["apple_track_id"],
                        reveal.preview_url,
                        reveal.model_dump_json(),
                    ),
                ),
                (
                    "INSERT INTO participant_rounds (room_code, guest_id, "
                    "position) VALUES (?, ?, ?)",
                    (code, guest, position),
                ),
            ]
        )
    await database.batch(statements)
    return await _summary(database, await _room(database, code), guest)


@router.get("/rooms/{code}", response_model=RoomSummary)
async def room_summary(code: str, request: Request, database: DB):
    return await _summary(database, await _room(database, code), _guest(request, required=False))


@router.post("/rooms/{code}/join", response_model=RoomState)
async def join_room(code: str, body: NamedRequest, request: Request, database: DB):
    _origin(request)
    guest = _guest(request)
    room = await _room(database, code)
    member = await database.fetch_one(
        "SELECT guest_id FROM participants WHERE room_code = ? AND guest_id = ?", (code, guest)
    )
    if member:
        return await _state(database, room, guest)
    joined = now()
    statements = [
        (
            "INSERT INTO participants SELECT code, ?, ?, ? FROM rooms WHERE "
            "code = ? AND closes_at > ? "
            "AND (SELECT COUNT(*) FROM participants WHERE room_code = ?) < 50 "
            "ON CONFLICT(room_code, guest_id) DO NOTHING",
            (guest, body.display_name, joined, code, joined, code),
        ),
        (
            "INSERT INTO participant_rounds (room_code, guest_id, position) "
            "SELECT rs.room_code, ?, rs.position "
            "FROM room_songs rs JOIN participants p ON p.room_code = "
            "rs.room_code AND p.guest_id = ? "
            "WHERE rs.room_code = ? ON CONFLICT(room_code, guest_id, position) DO NOTHING",
            (guest, guest, code),
        ),
    ]
    await database.batch(statements)
    if (
        await database.fetch_one(
            "SELECT guest_id FROM participants WHERE room_code = ? AND guest_id = ?", (code, guest)
        )
        is None
    ):
        raise HTTPException(409, "Challenge is closed or full")
    return await _state(database, room, guest)


@router.get("/rooms/{code}/me", response_model=RoomState)
async def room_me(code: str, request: Request, database: DB):
    return await _state(database, await _room(database, code), _guest(request))


@router.post("/rooms/{code}/actions", response_model=ActionResponse)
async def room_action(code: str, body: RoomAction, request: Request, database: DB):
    _origin(request)
    guest = _guest(request)
    room = await _room(database, code)
    state = await _state(database, room, guest)
    current = state["current_round"]
    if (
        current is None
        or current["position"] != body.position
        or current["version"] != body.expected_version
    ):
        raise HTTPException(409, "Round changed or challenge closed; reload the challenge")
    row = await database.fetch_one(
        "SELECT pr.*, rs.answer_apple_track_id, rs.reveal_json FROM participant_rounds pr "
        "JOIN room_songs rs USING(room_code, position) WHERE pr.room_code = "
        "? AND pr.guest_id = ? AND pr.position = ?",
        (code, guest, body.position),
    )
    if row is None:
        raise HTTPException(409, "Round changed; reload the challenge")
    clue = row["clue_number"]
    guesses = json.loads(row["guesses_json"])
    result = None
    if body.action == "guess":
        if body.song_id is None:
            raise HTTPException(422, "Choose a song")
        identity = await database.fetch_one(
            "SELECT * FROM songs WHERE id = ? AND enabled = 1", (body.song_id,)
        )
        if identity is None or not identity["apple_track_id"]:
            raise HTTPException(422, "Song unavailable")
        # Preserve identity alongside the public guess snapshot to survive catalog ID reuse.
        if any(guess.get("_apple_track_id") == identity["apple_track_id"] for guess in guesses):
            raise HTTPException(422, "You already guessed this song")
        guess = SongSearchResult.model_validate(
            {field: identity[field] for field in SongSearchResult.model_fields}
        ).model_dump()
        guess["_apple_track_id"] = identity["apple_track_id"]
        guesses.append(guess)
        if identity["apple_track_id"] == row["answer_apple_track_id"]:
            result = f"clue_{clue}"
    if result is None:
        if body.action == "give_up" or clue == 6:
            result = "not_guessed"
        else:
            clue += 1
    completed = now() if result else None
    changed = await database.execute(
        "UPDATE participant_rounds SET clue_number = ?, guesses_json = ?, "
        "result = ?, version = version + 1, completed_at = ? "
        "WHERE room_code = ? AND guest_id = ? AND position = ? AND version = ? AND result IS NULL "
        "AND EXISTS(SELECT 1 FROM rooms WHERE code = ? AND closes_at > ? "
        "AND delete_at > ? AND closes_at > unixepoch()) "
        "AND NOT EXISTS(SELECT 1 FROM participant_rounds earlier WHERE earlier.room_code = ? "
        "AND earlier.guest_id = ? AND earlier.position < ? AND earlier.result IS NULL)",
        (
            clue,
            json.dumps(guesses),
            result,
            completed,
            code,
            guest,
            body.position,
            body.expected_version,
            code,
            now(),
            now(),
            code,
            guest,
            body.position,
        ),
    )
    if changed != 1:
        raise HTTPException(409, "Round changed or challenge closed; reload the challenge")
    return {
        "state": await _state(database, room, guest),
        "round": {
            "position": body.position,
            "clue_number": clue,
            "guesses": _public_guesses(guesses),
            "version": body.expected_version + 1,
            "result": result,
            "reveal": json.loads(row["reveal_json"]) if result else None,
        },
    }


def _public_guesses(guesses):
    return [
        {key: value for key, value in guess.items() if not key.startswith("_")} for guess in guesses
    ]


async def cleanup_expired_rooms(database: Database, timestamp: int | None = None) -> int:
    cutoff = now() if timestamp is None else timestamp
    total = 0
    while True:
        deleted = await database.execute(
            "DELETE FROM rooms WHERE code IN (SELECT code FROM rooms WHERE "
            "delete_at <= ? LIMIT 100)",
            (cutoff,),
        )
        total += deleted
        if deleted < 100:
            return total
