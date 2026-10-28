"""Advisory canary for the current MusicBrainz JSON API contract."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request

import pytest

from soulseek.musicbrainz import parse_search_payloads

pytestmark = [pytest.mark.live, pytest.mark.musicbrainz]

USER_AGENT = (
    "MusicAssistant-Soulseek-Canary/1.0 "
    "(https://github.com/sproft/ytmusic-free-provider)"
)
LAST_REQUEST = 0.0


def get(entity: str, **params):
    global LAST_REQUEST
    query = urllib.parse.urlencode({**params, "fmt": "json"})
    request = urllib.request.Request(
        f"https://musicbrainz.org/ws/2/{entity}?{query}",
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    )
    for attempt in range(3):
        delay = 1.1 - (time.monotonic() - LAST_REQUEST)
        if delay > 0:
            time.sleep(delay)
        LAST_REQUEST = time.monotonic()
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.load(response)
        except urllib.error.HTTPError as err:
            if err.code not in (429, 503) or attempt == 2:
                raise
    raise AssertionError("MusicBrainz request retry loop ended unexpectedly")


def test_live_musicbrainz_search_returns_mbids_and_recording_lengths():
    artists = get("artist", query="The Beatles", limit=5)
    albums = get("release-group", query="Abbey Road AND artist:The Beatles", limit=5)
    tracks = get("recording", query="Come Together AND artist:The Beatles", limit=5)
    parsed = parse_search_payloads(artists, albums, tracks)
    assert parsed.artists
    assert parsed.albums
    assert parsed.tracks
    assert any(track.duration for track in parsed.tracks)

    artist = get(f"artist/{parsed.artists[0].item_id}")
    recording = get(
        f"recording/{parsed.tracks[0].item_id}",
        inc="artist-credits+releases+release-groups",
    )
    releases = get(
        "release-group",
        artist=artist["id"],
        inc="artist-credits",
        limit=5,
    )
    assert artist["id"] == parsed.artists[0].item_id
    assert recording["id"] == parsed.tracks[0].item_id
    assert releases["release-groups"]


def test_live_musicbrainz_release_lookup_exposes_media_recordings():
    matches = get(
        "release",
        query='release:"Abbey Road" AND artist:"The Beatles" AND status:official',
        limit=5,
    )
    assert matches["releases"]
    release = get(
        f"release/{matches['releases'][0]['id']}",
        inc="recordings+artist-credits+release-groups+media",
    )
    assert release["media"]
    assert any(medium.get("tracks") for medium in release["media"])
