import asyncio
import json

import pytest
from dataset.populate import initialize_database
from fastapi.testclient import TestClient

from app.database import SQLiteDatabase, connect, request_database
from app.main import app
from app.rooms import COOKIE, cleanup_expired_rooms


@pytest.fixture
def catalog(tmp_path):
    path = tmp_path / "rooms.sqlite3"
    initialize_database(path)
    with connect(path) as connection:
        for song_id in range(1, 5):
            connection.execute(
                "INSERT INTO songs "
                "(id,title,artist,release_year,popularity_score,apple_track_id,"
                "preview_url,enabled) "
                "VALUES (?, ?, 'Artist', 2000, 80, ?, ?, 1)",
                (song_id, f"Song {song_id}", f"apple-{song_id}", f"https://audio/{song_id}"),
            )
        connection.commit()

    async def database():
        with connect(path) as connection:
            yield SQLiteDatabase(connection)

    app.dependency_overrides[request_database] = database
    yield path
    app.dependency_overrides.clear()


def client():
    result = TestClient(app)
    assert result.post("/api/guest-session").status_code == 200
    return result


def create(c, count=2):
    response = c.post(
        "/api/rooms",
        json={
            "display_name": "  Player  ",
            "song_count": count,
            "filters": {
                "year_min": 1900,
                "year_max": 2100,
                "popularity_min": 0,
                "popularity_max": 100,
            },
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["code"]


def act(c, code, action, song_id=None, position=None, version=None):
    state = c.get(f"/api/rooms/{code}/me").json()
    current = state["current_round"]
    body = {
        "action": action,
        "position": position or current["position"],
        "expected_version": current["version"] if version is None else version,
    }
    if song_id:
        body["song_id"] = song_id
    return c.post(f"/api/rooms/{code}/actions", json=body)


def answer(path, code, position):
    with connect(path) as connection:
        row = connection.execute(
            "SELECT reveal_json FROM room_songs WHERE room_code=? AND position=?", (code, position)
        ).fetchone()
    return json.loads(row[0])["id"]


def test_resume_score_ties_and_open_join(catalog):
    c = client()
    code = create(c)
    state = c.get(f"/api/rooms/{code}/me").json()
    assert state["display_name"] == "Player"
    assert "reveal" not in state["current_round"]
    assert "song_id" not in state["current_round"]
    assert act(c, code, "guess", answer(catalog, code, 1)).json()["state"]["total_points"] == 6
    assert act(c, code, "skip").json()["round"]["clue_number"] == 2
    assert act(c, code, "guess", answer(catalog, code, 2)).json()["state"]["total_points"] == 11
    assert c.get(f"/api/rooms/{code}/me").json()["current_round"] is None
    other = client()
    assert other.post(f"/api/rooms/{code}/join", json={"display_name": "Player"}).status_code == 200
    assert (
        other.post(f"/api/rooms/{code}/join", json={"display_name": "Renamed"}).json()[
            "display_name"
        ]
        == "Player"
    )
    act(other, code, "guess", answer(catalog, code, 1))
    act(other, code, "skip")
    act(other, code, "guess", answer(catalog, code, 2))
    summary = c.get(f"/api/rooms/{code}").json()
    assert summary["status"] == "active"
    assert [item["rank"] for item in summary["leaderboard"]] == [1, 1]
    assert summary["participant_count"] == 2


def test_wrong_guess_duplicate_final_clue_and_cas(catalog):
    c = client()
    code = create(c, 1)
    wrong = next(song for song in range(1, 5) if song != answer(catalog, code, 1))
    first = act(c, code, "guess", wrong).json()
    assert first["round"]["result"] is None
    assert "_apple_track_id" not in first["state"]["current_round"]["guesses"][0]
    assert act(c, code, "guess", wrong).status_code == 422
    assert act(c, code, "skip", version=0).status_code == 409
    assert c.get(f"/api/rooms/{code}/me").json()["current_round"]["version"] == 1
    for _ in range(4):
        assert act(c, code, "skip").status_code == 200
    final = act(c, code, "skip").json()
    assert final["round"]["result"] == "not_guessed"
    assert final["state"]["current_round"] is None
    assert final["state"]["total_points"] == 0
    assert act(c, code, "skip", position=1, version=6).status_code == 409


def test_future_round_and_deadline(catalog):
    c = client()
    code = create(c)
    assert act(c, code, "give_up", position=2).status_code == 409
    with connect(catalog) as connection:
        connection.execute("UPDATE rooms SET closes_at=0 WHERE code=?", (code,))
        connection.commit()
    state = c.get(f"/api/rooms/{code}/me").json()
    assert state["closed"] and state["current_round"] is None
    assert len(state["results"]) == 2
    assert all(result["result"] == "not_guessed" for result in state["results"])
    assert act(c, code, "skip", position=1, version=0).status_code == 409
    other = client()
    assert other.post(f"/api/rooms/{code}/join", json={"display_name": "New"}).status_code == 409


def test_cookie_origin_and_membership_security(catalog):
    c = client()
    guest = c.post("/api/guest-session").json()["guest_id"]
    assert c.post("/api/guest-session").json()["guest_id"] == guest
    code = create(c)
    other = client()
    assert other.get(f"/api/rooms/{code}/me").status_code == 403
    assert (
        c.post(
            f"/api/rooms/{code}/join",
            json={"display_name": "New"},
            headers={"Origin": "https://evil.test"},
        ).status_code
        == 403
    )
    assert c.post("/api/guest-session", headers={"sec-fetch-site": "cross-site"}).status_code == 403
    token = c.cookies.get(COOKIE)
    c.cookies.clear()
    c.cookies.set(COOKIE, token[:-1] + ("a" if token[-1] != "a" else "b"))
    assert c.get(f"/api/rooms/{code}/me").status_code == 401
    c.cookies.clear()
    c.cookies.set(COOKIE, token.split(".")[0] + ".0." + token.split(".")[2])
    assert c.get(f"/api/rooms/{code}/me").status_code == 401


def test_snapshots_survive_catalog_replacement_and_cleanup(catalog):
    c = client()
    code = create(c, 1)
    original = answer(catalog, code, 1)
    with connect(catalog) as connection:
        identity = connection.execute(
            "SELECT apple_track_id FROM songs WHERE id=?", (original,)
        ).fetchone()[0]
        connection.execute("DELETE FROM songs")
        connection.execute(
            "INSERT INTO "
            "songs(id,title,artist,release_year,popularity_score,apple_track_id,preview_url) VALUES (99,'Updated','Artist',2000,80,?,'https://new')",
            (identity,),
        )
        connection.commit()
    result = act(c, code, "guess", 99).json()
    assert result["round"]["result"] == "clue_1"
    assert result["round"]["reveal"]["title"] == f"Song {original}"
    with connect(catalog) as connection:
        connection.execute("UPDATE rooms SET closes_at=0,delete_at=1")
        connection.commit()
    assert c.get(f"/api/rooms/{code}").status_code == 404
    with connect(catalog) as connection:
        assert asyncio.run(cleanup_expired_rooms(SQLiteDatabase(connection), 1)) == 1
        for table in ["rooms", "room_songs", "participants", "participant_rounds"]:
            assert connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


def test_capacity_and_atomic_rollback(catalog):
    c = client()
    code = create(c, 1)
    with connect(catalog) as connection:
        connection.executemany(
            "INSERT INTO participants VALUES (?, ?, 'Guest', 0)",
            [(code, f"guest-{index}") for index in range(49)],
        )
        connection.commit()
    other = client()
    assert other.post(f"/api/rooms/{code}/join", json={"display_name": "New"}).status_code == 409
    with connect(catalog) as connection:
        db = SQLiteDatabase(connection)
        with pytest.raises(Exception):
            asyncio.run(
                db.batch(
                    [
                        ("INSERT INTO participants VALUES (?, 'rollback', 'Test', 0)", (code,)),
                        ("INSERT INTO room_songs VALUES (?,1,'duplicate','x','{}')", (code,)),
                    ]
                )
            )
        assert (
            connection.execute("SELECT * FROM participants WHERE guest_id='rollback'").fetchone()
            is None
        )


def test_validation_and_distinct_songs(catalog):
    c = client()
    code = create(c, 4)
    with connect(catalog) as connection:
        songs = connection.execute(
            "SELECT answer_apple_track_id FROM room_songs WHERE room_code=?", (code,)
        ).fetchall()
        assert len({row[0] for row in songs}) == 4
    for body in [
        {"display_name": " ", "filters": {}},
        {"display_name": "x", "song_count": 21, "filters": {}},
    ]:
        assert c.post("/api/rooms", json=body).status_code == 422


def test_concurrent_actions_consume_one_clue_and_join_is_idempotent(catalog):
    from concurrent.futures import ThreadPoolExecutor

    c = client()
    code = create(c)
    body = {"position": 1, "expected_version": 0, "action": "skip"}
    with ThreadPoolExecutor(max_workers=2) as pool:
        replies = list(
            pool.map(lambda _: c.post(f"/api/rooms/{code}/actions", json=body), range(2))
        )
    assert sorted(reply.status_code for reply in replies) == [200, 409]
    assert c.get(f"/api/rooms/{code}/me").json()["current_round"]["clue_number"] == 2
    other = client()
    with ThreadPoolExecutor(max_workers=2) as pool:
        replies = list(
            pool.map(
                lambda _: other.post(f"/api/rooms/{code}/join", json={"display_name": "Guest"}),
                range(2),
            )
        )
    assert all(reply.status_code == 200 for reply in replies)
    with connect(catalog) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM participants WHERE room_code=?", (code,)
            ).fetchone()[0]
            == 2
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM participant_rounds WHERE room_code=?", (code,)
            ).fetchone()[0]
            == 4
        )


def test_tie_breaks_use_correct_count_then_early_clues(catalog):
    from app.rooms import _score

    # Equal points: two clue-4 answers rank above one clue-1 answer.
    assert _score([{"result": "clue_4"}, {"result": "clue_4"}]) > _score([{"result": "clue_1"}])
    # Equal points and correct counts: the clue-1 histogram breaks the tie.
    assert _score([{"result": "clue_1"}, {"result": "clue_3"}]) > _score(
        [{"result": "clue_2"}, {"result": "clue_2"}]
    )


def test_cleanup_batches_and_catalog_export_excludes_room_guests(catalog, tmp_path):
    from dataset.export_d1 import export_d1

    c = client()
    code = create(c, 1)
    output = tmp_path / "export.sql"
    export_d1(catalog, output)
    exported = output.read_text()
    assert "INSERT INTO rooms" not in exported
    assert "INSERT INTO participants" not in exported
    with connect(catalog) as connection:
        connection.executemany(
            "INSERT INTO rooms VALUES (?, 'private', '{}', 1, 0, 0, 1)",
            [(f"{index:08d}",) for index in range(205)],
        )
        connection.commit()
        assert asyncio.run(cleanup_expired_rooms(SQLiteDatabase(connection), 1)) == 205
        assert (
            connection.execute("SELECT COUNT(*) FROM rooms WHERE code=?", (code,)).fetchone()[0]
            == 1
        )


def test_worker_requires_strong_session_secret(catalog):
    from types import SimpleNamespace

    from fastapi import HTTPException, Request

    from app.rooms import _secret

    for environment in [SimpleNamespace(), SimpleNamespace(SONGUESS_GUEST_SECRET="short")]:
        request = Request({"type": "http", "env": environment})
        with pytest.raises(HTTPException) as error:
            _secret(request)
        assert error.value.status_code == 503
    assert (
        _secret(Request({"type": "http", "env": SimpleNamespace(SONGUESS_GUEST_SECRET="x" * 32)}))
        == b"x" * 32
    )


def test_d1_mutations_use_atomic_batch_and_report_changes():
    from types import SimpleNamespace

    from app.database import D1Database

    class Statement:
        def __init__(self, sql):
            self.sql = sql
            self.parameters = ()

        def bind(self, *parameters):
            self.parameters = parameters
            return self

        async def run(self):
            return SimpleNamespace(meta=SimpleNamespace(changes=3))

    class Binding:
        def __init__(self):
            self.statements = []

        def prepare(self, sql):
            return Statement(sql)

        async def batch(self, statements):
            self.statements = statements
            return [
                SimpleNamespace(meta=SimpleNamespace(changes=index))
                for index, _ in enumerate(statements)
            ]

    binding = Binding()
    db = D1Database(binding)
    assert asyncio.run(db.execute("DELETE FROM rooms WHERE delete_at <= ?", (1,))) == 3
    assert asyncio.run(
        db.batch(
            [
                ("INSERT INTO rooms VALUES (?)", ("ABCD1234",)),
                ("INSERT INTO participants VALUES (?)", ("guest",)),
            ]
        )
    ) == [0, 1]
    assert [statement.parameters for statement in binding.statements] == [("ABCD1234",), ("guest",)]


def test_deadline_crossing_during_action_cannot_commit(catalog, monkeypatch):
    c = client()
    code = create(c, 1)
    execute = SQLiteDatabase.execute

    async def close_before_write(database, statement, parameters=()):
        if statement.startswith("UPDATE participant_rounds"):
            database.connection.execute("UPDATE rooms SET closes_at=0 WHERE code=?", (code,))
            database.connection.commit()
        return await execute(database, statement, parameters)

    monkeypatch.setattr(SQLiteDatabase, "execute", close_before_write)
    assert act(c, code, "guess", answer(catalog, code, 1)).status_code == 409
    with connect(catalog) as connection:
        row = connection.execute(
            "SELECT result, version FROM participant_rounds WHERE room_code=?", (code,)
        ).fetchone()
    assert row["result"] is None and row["version"] == 0
