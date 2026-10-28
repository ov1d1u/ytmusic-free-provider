"""Fixture-driven tests for the MusicBrainz catalog adapter."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from music_assistant_models.enums import AlbumType
from music_assistant_models.errors import MediaNotFoundError

import soulseek
import soulseek.musicbrainz as musicbrainz_module
from soulseek.musicbrainz import (
    MusicBrainzClient,
    MusicBrainzError,
    MusicBrainzItem,
    parse_artist,
    parse_recording,
    parse_release_group,
    parse_release_tracks,
    parse_search_payloads,
)

ARTIST_ID = "b10bbbfc-cf9e-42e0-be17-e2c3e1d2600d"
GROUP_ID = "f2e2c3e1-a111-4aaa-8bbb-123456789abc"
RECORDING_ID = "a1234567-b123-c123-d123-e123456789ab"


def artist_credit(name: str = "The Beatles"):
    return [{"name": name, "artist": {"id": ARTIST_ID, "name": name}}]


def release_group(**extra):
    return {
        "id": GROUP_ID,
        "title": "Abbey Road",
        "primary-type": "Album",
        "first-release-date": "1969-09-26",
        "artist-credit": artist_credit(),
        **extra,
    }


def recording(**extra):
    return {
        "id": RECORDING_ID,
        "title": "Come Together",
        "length": 259000,
        "artist-credit": artist_credit(),
        "releases": [
            {
                "id": "release-id",
                "title": "Abbey Road",
                "status": "Official",
                "date": "1969-09-26",
                "release-group": release_group(),
            }
        ],
        **extra,
    }


def test_search_models_use_mbids_duration_cover_art_and_deduplicate():
    parsed = parse_search_payloads(
        {"artists": [{"id": ARTIST_ID, "name": "The Beatles"}]},
        {"release-groups": [release_group(), release_group()]},
        {"recordings": [recording(), recording()]},
    )

    assert parsed.artists[0].stable_id == ARTIST_ID
    assert parsed.albums[0].stable_id == GROUP_ID
    assert parsed.albums[0].year == 1969
    assert parsed.albums[0].album_type == "Album"
    assert parsed.albums[0].artwork.endswith(f"/{GROUP_ID}/front-500")
    assert len(parsed.tracks) == 1
    assert parsed.tracks[0].stable_id == RECORDING_ID
    assert parsed.tracks[0].duration == 259
    assert parsed.tracks[0].album_id == GROUP_ID
    assert parsed.tracks[0].artist_id == ARTIST_ID


def test_artist_credit_preserves_join_phrases():
    value = recording(
        **{
            "artist-credit": [
                {
                    "name": "Artist One",
                    "joinphrase": " feat. ",
                    "artist": {"id": ARTIST_ID, "name": "Artist One"},
                },
                {
                    "name": "Artist Two",
                    "artist": {"id": "second", "name": "Artist Two"},
                },
            ]
        }
    )
    parsed = parse_recording(value)
    assert parsed is not None
    assert parsed.artist_name == "Artist One feat. Artist Two"
    assert parsed.artist_id == ARTIST_ID


def test_release_media_preserves_disc_track_order_and_fills_track_length():
    album = parse_release_group(release_group())
    assert album is not None
    release = {
        "media": [
            {
                "position": 2,
                "tracks": [
                    {
                        "position": 3,
                        "length": 201400,
                        "artist-credit": artist_credit(),
                        "recording": {
                            "id": RECORDING_ID,
                            "title": "Because",
                        },
                    }
                ],
            }
        ]
    }
    tracks = parse_release_tracks(release, album)
    assert len(tracks) == 1
    assert tracks[0].track_number == 3
    assert tracks[0].disc_number == 2
    assert tracks[0].duration == 201
    assert tracks[0].album_id == GROUP_ID
    assert tracks[0].artwork == album.artwork


def test_malformed_lengths_are_ignored_and_shapes_fail_clearly():
    parsed = parse_recording(recording(length="not-a-number"))
    assert parsed is not None
    assert parsed.duration is None
    with pytest.raises(MusicBrainzError, match="artists list"):
        parse_search_payloads({}, {"release-groups": []}, {"recordings": []})
    album = MusicBrainzItem(kind="album", item_id=GROUP_ID, name="Album")
    with pytest.raises(MusicBrainzError, match="media"):
        parse_release_tracks({}, album)


def test_sparse_items_without_identity_are_skipped():
    assert parse_artist({"name": "No MBID"}) is None
    assert parse_release_group({"id": GROUP_ID}) is None
    assert parse_recording({"id": RECORDING_ID}) is None


def test_release_selection_prefers_official_then_earliest():
    releases = [
        {"id": "bootleg", "status": "Bootleg", "date": "1968-01-01"},
        {"id": "later", "status": "Official", "date": "1970-01-01"},
        {"id": "first", "status": "Official", "date": "1969-01-01"},
    ]
    assert MusicBrainzClient._select_release(releases) == "first"


def test_provider_maps_musicbrainz_release_group_type_and_mbid():
    provider = soulseek.SoulseekProvider(mass=None, manifest=None, config=None)
    parsed = parse_release_group(release_group())
    assert parsed is not None
    album = provider._album(parsed)
    assert album.item_id == GROUP_ID
    assert album.album_type == AlbumType.ALBUM
    assert next(iter(album.provider_mappings)).item_id == GROUP_ID


def test_album_page_looks_up_selected_release_and_returns_ordered_tracks():
    client = MusicBrainzClient(mass=None)
    calls: list[tuple[str, dict]] = []

    async def request(entity, params):
        calls.append((entity, params))
        if entity.startswith("release-group/"):
            return release_group(
                releases=[
                    {
                        "id": "chosen-release",
                        "status": "Official",
                        "date": "1969-09-26",
                    }
                ]
            )
        return {
            "media": [
                {
                    "position": 1,
                    "tracks": [
                        {"position": 1, "recording": recording(releases=[])},
                    ],
                }
            ]
        }

    client._request = request
    album, tracks = asyncio.run(client.album_page(GROUP_ID))
    assert album.item_id == GROUP_ID
    assert [track.item_id for track in tracks] == [RECORDING_ID]
    assert calls[1][0] == "release/chosen-release"
    assert "recordings" in calls[1][1]["inc"]


def test_artist_page_paginates_release_groups_and_uses_recording_search():
    client = MusicBrainzClient(mass=None)
    calls: list[tuple[str, dict]] = []

    async def request(entity, params):
        calls.append((entity, params))
        if entity.startswith("artist/"):
            return {"id": ARTIST_ID, "name": "The Beatles"}
        if entity == "release-group":
            return {"release-group-count": 1, "release-groups": [release_group()]}
        return {"recordings": [recording()]}

    client._request = request
    artist, albums, tracks = asyncio.run(client.artist_page(ARTIST_ID))
    assert artist.item_id == ARTIST_ID
    assert [album.item_id for album in albums] == [GROUP_ID]
    assert [track.item_id for track in tracks] == [RECORDING_ID]
    recording_call = next(call for call in calls if call[0] == "recording")
    assert recording_call[1]["query"] == f"arid:{ARTIST_ID}"


def test_release_group_browse_fetches_every_page():
    client = MusicBrainzClient(mass=None)
    offsets: list[int] = []

    async def request(entity, params):
        assert entity == "release-group"
        offsets.append(params["offset"])
        start = params["offset"]
        count = 100 if start == 0 else 1
        return {
            "release-group-count": 101,
            "release-groups": [
                release_group(id=f"group-{index}")
                for index in range(start, start + count)
            ],
        }

    client._request = request
    groups = asyncio.run(client._all_release_groups(ARTIST_ID))
    assert offsets == [0, 100]
    assert len(groups) == 101


class FakeResponse:
    def __init__(self, status, payload=None):
        self.status = status
        self.payload = payload or {}
        self.headers = {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    def raise_for_status(self):
        if self.status >= 400:
            raise RuntimeError(f"HTTP {self.status}")

    async def json(self, **_kwargs):
        return self.payload


class FakeSession:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return next(self.responses)


def test_request_sends_identity_retries_503_and_uses_json(monkeypatch):
    monkeypatch.setattr(musicbrainz_module, "REQUEST_INTERVAL_SECONDS", 0)
    session = FakeSession([FakeResponse(503), FakeResponse(200, {"artists": []})])
    client = MusicBrainzClient(SimpleNamespace(http_session=session))
    result = asyncio.run(client._request("artist", {"query": "test"}))
    assert result == {"artists": []}
    assert len(session.calls) == 2
    _url, kwargs = session.calls[0]
    assert kwargs["params"]["fmt"] == "json"
    assert kwargs["headers"]["User-Agent"].startswith("MusicAssistant-Soulseek/")
    assert kwargs["timeout"] == 15


def test_request_pacing_serializes_concurrent_starts(monkeypatch):
    clock = [10.0]
    sleeps: list[float] = []

    async def sleep(delay):
        sleeps.append(delay)
        clock[0] += delay

    monkeypatch.setattr(musicbrainz_module.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(musicbrainz_module.asyncio, "sleep", sleep)
    client = MusicBrainzClient(mass=None)

    async def run():
        await asyncio.gather(client._pace(), client._pace(), client._pace())

    asyncio.run(run())
    assert sleeps == pytest.approx([1.1, 1.1])


def test_request_maps_404_without_retry(monkeypatch):
    monkeypatch.setattr(musicbrainz_module, "REQUEST_INTERVAL_SECONDS", 0)
    session = FakeSession([FakeResponse(404)])
    client = MusicBrainzClient(SimpleNamespace(http_session=session))
    with pytest.raises(MediaNotFoundError, match="was not found"):
        asyncio.run(client._request(f"artist/{ARTIST_ID}", {}))
    assert len(session.calls) == 1


def test_request_maps_bounded_server_failure(monkeypatch):
    monkeypatch.setattr(musicbrainz_module, "REQUEST_INTERVAL_SECONDS", 0)
    session = FakeSession([FakeResponse(500), FakeResponse(500), FakeResponse(500)])
    client = MusicBrainzClient(SimpleNamespace(http_session=session))
    with pytest.raises(MusicBrainzError, match="HTTP 500"):
        asyncio.run(client._request("artist", {"query": "test"}))
    assert len(session.calls) == 3


def test_normalized_results_round_trip_through_cache_serialization():
    result = parse_search_payloads(
        {"artists": [{"id": ARTIST_ID, "name": "The Beatles"}]},
        {"release-groups": [release_group()]},
        {"recordings": [recording()]},
    )
    restored = type(result).from_dict(result.to_dict())
    assert restored == result
