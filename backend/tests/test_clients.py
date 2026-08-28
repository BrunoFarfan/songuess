from dataset import clients


def test_musicbrainz_throttle_spaces_request_starts_not_response_ends(monkeypatch) -> None:
    moments = iter([10.5, 11.05])
    sleeps: list[float] = []
    monkeypatch.setattr(clients, "_MUSICBRAINZ_LAST_REQUEST_STARTED", 10.0)
    monkeypatch.setattr(clients.time, "monotonic", lambda: next(moments))
    monkeypatch.setattr(clients.time, "sleep", sleeps.append)

    clients._throttle_musicbrainz("https://musicbrainz.org/ws/2/release?artist=x")

    assert sleeps == [0.5500000000000007]
    assert clients._MUSICBRAINZ_LAST_REQUEST_STARTED == 11.05


def test_musicbrainz_throttle_ignores_other_providers(monkeypatch) -> None:
    monkeypatch.setattr(
        clients.time,
        "sleep",
        lambda _: (_ for _ in ()).throw(AssertionError("unexpected throttle")),
    )

    clients._throttle_musicbrainz("https://api.listenbrainz.org/1/popularity/test")
