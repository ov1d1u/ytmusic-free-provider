"""Regression tests for mixes, song radio and editorial playlists (issue #47).

"RD" is not one family. Which endpoint can answer depends on which kind it is,
and all of the following were measured against the live services:

* song radio ``RD<videoId>`` / ``RDAMVM<videoId>``: ``get_playlist`` raises a
  ``KeyError``, ``playlist?list=RD...`` is rejected with "This playlist type is
  unviewable", and only the watch endpoint answers (147 tracks)
* personal mixes ``RDTMAK5uy_...`` ("My Supermix"): carry no seed video in the
  id, so a watch URL cannot be built for them without one
* editorial playlists ``RDCLAK5uy_...`` ("'80s Pop"): not radio at all. These
  read normally through ``get_playlist`` and are most of what the home feed
  hands out. The watch endpoint answers for them too, but only with a queue's
  worth, so sending them there loses tracks: 200 the ordinary way, 101 the
  watch way.

The original defect was that a mix resolved to zero tracks and playback had
nothing to start, while every failure on the way was discarded without a log
line.

These tests pin the routing so the URL shapes cannot be conflated again, pin
which family goes to which endpoint, and pin the reshaping that the watch
endpoint's track format requires.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

import ytmusic_free as ytm


# Real ids, all checked against the live services. The Supermix one is the id
# reported in issue #47; EDITORIAL_ID is "'80s Pop", which get_playlist returns
# 200 tracks for and the watch endpoint only 101.
SUPERMIX_ID = "RDTMAK5uy_kset8DisdE7LSD4TNjEVvrKRTmG7a56sY"
EDITORIAL_ID = "RDCLAK5uy_k1Wu8QbZASiGVqr1wmie9NIYo38aBqscQ"
SONG_RADIO_ID = "RDdQw4w9WgXcQ"
VIDEO_RADIO_ID = "RDAMVMdQw4w9WgXcQ"
STATIC_ID = "PLFgquLnL59alW3xmYiWRaoz0oM3H17Lth"


# ---------------------------------------------------------------------------
# Id classification
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "playlist_id", [SUPERMIX_ID, EDITORIAL_ID, SONG_RADIO_ID, VIDEO_RADIO_ID]
)
def test_radio_ids_are_recognised(playlist_id):
    assert ytm._is_radio_playlist_id(playlist_id)
    assert ytm._is_radio_playlist_id(f"VL{playlist_id}"), "VL browse prefix must be stripped first"


@pytest.mark.parametrize("playlist_id", [STATIC_ID, f"VL{STATIC_ID}", "OLAK5uy_abc123"])
def test_ordinary_playlist_ids_are_not_treated_as_radio(playlist_id):
    assert not ytm._is_radio_playlist_id(playlist_id)


@pytest.mark.parametrize(
    "playlist_id", [SUPERMIX_ID, SONG_RADIO_ID, VIDEO_RADIO_ID, f"VL{SUPERMIX_ID}"]
)
def test_only_the_watch_endpoint_answers_for_radio_proper(playlist_id):
    assert ytm._is_watch_only_playlist_id(playlist_id)


@pytest.mark.parametrize("playlist_id", [EDITORIAL_ID, f"VL{EDITORIAL_ID}", STATIC_ID])
def test_editorial_and_static_playlists_are_not_watch_only(playlist_id):
    """RDCLAK5uy_ is an ordinary playlist wearing an RD prefix.

    Sending it to the watch endpoint costs tracks, so it must not be classed
    with song radio however much the prefix suggests otherwise.
    """
    assert not ytm._is_watch_only_playlist_id(playlist_id)


def test_song_radio_ids_yield_their_seed_video():
    assert ytm._radio_seed_video_id(SONG_RADIO_ID) == "dQw4w9WgXcQ"
    assert ytm._radio_seed_video_id(VIDEO_RADIO_ID) == "dQw4w9WgXcQ"


@pytest.mark.parametrize("playlist_id", [SUPERMIX_ID, EDITORIAL_ID])
def test_seedless_ids_carry_no_seed(playlist_id):
    """The length anchor is what separates these from song radio.

    ``RDTMAK5uy_kset8Dis...`` is far longer than a video id, so treating the
    tail as one would build a URL for a video that does not exist.
    """
    assert ytm._radio_seed_video_id(playlist_id) is None


# ---------------------------------------------------------------------------
# URL routing: the actual bug
# ---------------------------------------------------------------------------


def test_song_radio_uses_the_watch_form_without_help(provider):
    url = provider._yt_playlist_url(SONG_RADIO_ID)
    assert url == f"https://www.youtube.com/watch?v=dQw4w9WgXcQ&list={SONG_RADIO_ID}"


def test_curated_mix_uses_the_watch_form_with_a_supplied_seed(provider):
    url = provider._yt_playlist_url(SUPERMIX_ID, "abcdefghijk")
    assert url == f"https://www.youtube.com/watch?v=abcdefghijk&list={SUPERMIX_ID}"


@pytest.mark.parametrize(
    ("playlist_id", "seed"),
    [
        (SUPERMIX_ID, "abcdefghijk"),
        (EDITORIAL_ID, "abcdefghijk"),
        (SONG_RADIO_ID, None),
        (VIDEO_RADIO_ID, None),
        (f"VL{SONG_RADIO_ID}", None),
    ],
)
def test_no_radio_id_is_ever_requested_as_a_plain_playlist(provider, playlist_id, seed):
    """Guard the root cause rather than one example of it.

    ``playlist?list=RD...`` is the exact request YouTube answers with "This
    playlist type is unviewable", so no radio id that we can build a watch URL
    for may come out in that shape.
    """
    url = provider._yt_playlist_url(playlist_id, seed)
    assert "/playlist?list=" not in url, f"{playlist_id} would be unviewable at {url}"
    assert url.startswith("https://www.youtube.com/watch?v=")


def test_static_playlists_keep_the_plain_form(provider):
    """The watch form must not leak onto ordinary playlists."""
    assert provider._yt_playlist_url(STATIC_ID) == (
        f"https://www.youtube.com/playlist?list={STATIC_ID}"
    )
    # A seed is meaningless here and must be ignored rather than applied.
    assert provider._yt_playlist_url(STATIC_ID, "abcdefghijk") == (
        f"https://www.youtube.com/playlist?list={STATIC_ID}"
    )


def test_vl_prefix_is_stripped_from_the_list_parameter(provider):
    assert provider._yt_playlist_url(f"VL{STATIC_ID}") == (
        f"https://www.youtube.com/playlist?list={STATIC_ID}"
    )


# ---------------------------------------------------------------------------
# Watch-endpoint track reshaping
# ---------------------------------------------------------------------------


def _watch_track(video_id="vid1", length="3:32"):
    """A track shaped the way get_watch_playlist really returns them."""
    return {
        "videoId": video_id,
        "title": "Together Forever",
        "length": length,
        "artists": [{"name": "Rick Astley", "id": "UCwZEU0wAwIyZb4x5G_KJp2w"}],
        # Sizes copied from a real get_watch_playlist response. The largest has
        # to clear _parse_thumbnails' 500px floor or no artwork survives.
        "thumbnail": [
            {"url": "https://i.ytimg.com/vi/x/hq720.jpg?sqp=a", "width": 400, "height": 225},
            {"url": "https://i.ytimg.com/vi/x/hq720.jpg?sqp=b", "width": 800, "height": 450},
            {"url": "https://i.ytimg.com/vi/x/hq720.jpg?sqp=c", "width": 853, "height": 479},
        ],
        "videoType": "MUSIC_VIDEO_TYPE_ATV",
    }


def test_watch_track_duration_is_converted_from_the_clock_string():
    """``_parse_track`` only reads numeric duration keys.

    The watch endpoint reports ``length`` as "3:32", so without the reshaping
    every mix track renders as 0:00.
    """
    normalized = ytm._normalize_watch_track(_watch_track(length="3:32"))
    assert normalized["duration"] == 212


def test_watch_track_artwork_key_is_renamed():
    """Artwork arrives under ``thumbnail``; ``_parse_track`` reads ``thumbnails``."""
    normalized = ytm._normalize_watch_track(_watch_track())
    assert normalized["thumbnails"] == normalized["thumbnail"]


def test_watch_track_reshaping_does_not_clobber_existing_values():
    original = _watch_track()
    original["duration"] = 999
    original["thumbnails"] = [{"url": "keep-me"}]
    normalized = ytm._normalize_watch_track(original)
    assert normalized["duration"] == 999
    assert normalized["thumbnails"] == [{"url": "keep-me"}]


def test_watch_track_reshaping_leaves_the_caller_dict_alone():
    original = _watch_track()
    ytm._normalize_watch_track(original)
    assert "duration" not in original
    assert "thumbnails" not in original


def test_watch_track_survives_a_missing_length():
    broken = _watch_track()
    del broken["length"]
    normalized = ytm._normalize_watch_track(broken)
    assert "duration" not in normalized


def test_parsed_mix_track_ends_up_with_a_duration(provider):
    """End to end through the real parser, which is what MA consumes."""
    track = provider._parse_track(ytm._normalize_watch_track(_watch_track()))
    assert track.duration == 212
    assert track.metadata.images


# ---------------------------------------------------------------------------
# get_playlist_tracks routing
# ---------------------------------------------------------------------------


def test_mix_tracks_come_from_the_watch_endpoint(provider):
    """The fix for issue #47: mixes resolve through get_watch_playlist."""
    mock = MagicMock()
    mock.get_watch_playlist = MagicMock(
        return_value={"tracks": [_watch_track("vid1"), _watch_track("vid2")]}
    )
    mock.get_playlist = MagicMock(side_effect=AssertionError("get_playlist must not be used"))
    provider._ytmusic = mock

    tracks = asyncio.run(provider.get_playlist_tracks(SUPERMIX_ID))

    assert [t.item_id for t in tracks] == ["vid1", "vid2"]
    assert [t.position for t in tracks] == [1, 2]
    assert all(t.duration == 212 for t in tracks)
    kwargs = mock.get_watch_playlist.call_args.kwargs
    assert kwargs["playlistId"] == SUPERMIX_ID
    assert kwargs["limit"] == ytm.RADIO_PLAYLIST_LIMIT


def test_mix_lookup_strips_the_vl_prefix(provider):
    mock = MagicMock()
    mock.get_watch_playlist = MagicMock(return_value={"tracks": [_watch_track("vid1")]})
    provider._ytmusic = mock

    asyncio.run(provider.get_playlist_tracks(f"VL{SUPERMIX_ID}"))

    assert mock.get_watch_playlist.call_args.kwargs["playlistId"] == SUPERMIX_ID


def test_static_playlists_do_not_use_the_watch_endpoint(provider):
    """Ordinary playlists must keep their existing path untouched."""
    mock = MagicMock()
    # Deliberately not side_effect=AssertionError: the provider catches broadly
    # enough to swallow one raised in here, so the count is what proves it.
    mock.get_watch_playlist = MagicMock(return_value={"tracks": [_watch_track("vid_watch")]})
    mock.get_playlist = MagicMock(
        return_value={
            "trackCount": 1,
            "tracks": [
                {"videoId": "vid1", "title": "Song", "artists": [{"id": "UC1", "name": "A"}]}
            ],
        }
    )
    provider._ytmusic = mock

    tracks = asyncio.run(provider.get_playlist_tracks(STATIC_ID))
    mock.get_watch_playlist.assert_not_called()
    assert [t.item_id for t in tracks] == ["vid1"]


def _numbered_playlist(count):
    """An ordinary get_playlist response with ``count`` distinct tracks."""
    return {
        "trackCount": count,
        "tracks": [
            {"videoId": f"vid{n}", "title": f"Song {n}", "artists": [{"id": "UC1", "name": "A"}]}
            for n in range(count)
        ],
    }


def test_editorial_playlists_keep_the_ordinary_path(provider):
    """RDCLAK5uy_ ids read fully through get_playlist, so they must go there.

    The watch endpoint answers for them, which is why routing them with the
    rest of the RD family looked correct, but it stops at a queue's length.
    Measured live on "'80s Pop": 200 tracks the ordinary way, 101 this way. The
    difference was 99 tracks silently missing from the playlist.
    """
    mock = MagicMock()
    # Assert on the call count rather than raising from the stub: the provider
    # catches broadly enough to swallow an AssertionError raised in here, which
    # would leave this passing whatever the routing does.
    mock.get_watch_playlist = MagicMock(return_value={"tracks": [_watch_track("vid_watch")]})
    mock.get_playlist = MagicMock(return_value=_numbered_playlist(150))
    provider._ytmusic = mock

    tracks = asyncio.run(provider.get_playlist_tracks(EDITORIAL_ID))

    mock.get_watch_playlist.assert_not_called()
    assert len(tracks) == 150, "the full playlist must survive, not a queue's worth"
    assert mock.get_playlist.call_args.kwargs["limit"] is None


def test_editorial_playlists_still_fall_back_to_the_watch_endpoint(provider):
    """Excluding them from watch-first must not cut them off from it entirely.

    An RDCLAK5uy_ id that the ordinary path cannot read (auth-bound, or simply
    dead) should still get its one chance at the radio endpoint before yt-dlp.
    """
    mock = MagicMock()
    mock.get_playlist = MagicMock(side_effect=KeyError("contents"))
    mock.get_watch_playlist = MagicMock(return_value={"tracks": [_watch_track("vid7")]})
    provider._ytmusic = mock
    provider._get_playlist_tracks_via_ytdlp = _empty_fallback

    tracks = asyncio.run(provider.get_playlist_tracks(EDITORIAL_ID))

    assert [t.item_id for t in tracks] == ["vid7"]
    # The ordinary path has to have been tried first, or this is just the old
    # watch-first routing passing under a new name.
    mock.get_playlist.assert_called_once()


def test_the_watch_endpoint_is_never_asked_twice_for_one_id(provider):
    """The fallback above must not re-run the attempt watch-first already made."""
    mock = MagicMock()
    mock.get_playlist = MagicMock(side_effect=KeyError("contents"))
    mock.get_watch_playlist = MagicMock(return_value={"tracks": []})
    provider._ytmusic = mock
    provider._get_playlist_tracks_via_ytdlp = _empty_fallback

    asyncio.run(provider.get_playlist_tracks(SUPERMIX_ID))

    assert mock.get_watch_playlist.call_count == 1


def test_mix_falls_back_when_the_watch_endpoint_returns_nothing(provider):
    """An empty radio result must not strand the playlist.

    Falling through keeps the previous behaviour reachable for anything
    RD-prefixed that happens to read as an ordinary playlist.
    """
    mock = MagicMock()
    mock.get_watch_playlist = MagicMock(return_value={"tracks": []})
    mock.get_playlist = MagicMock(
        return_value={
            "trackCount": 1,
            "tracks": [
                {"videoId": "vid9", "title": "Song", "artists": [{"id": "UC1", "name": "A"}]}
            ],
        }
    )
    provider._ytmusic = mock

    tracks = asyncio.run(provider.get_playlist_tracks(SUPERMIX_ID))
    assert [t.item_id for t in tracks] == ["vid9"]


def test_watch_endpoint_failure_is_logged_not_swallowed(provider, caplog):
    """The silence is half the bug: the reporter saw a stall and an empty log."""
    mock = MagicMock()
    mock.get_watch_playlist = MagicMock(side_effect=KeyError("endpoint"))
    mock.get_playlist = MagicMock(side_effect=KeyError("contents"))
    provider._ytmusic = mock
    provider._get_playlist_tracks_via_ytdlp = _empty_fallback

    with caplog.at_level("WARNING"):
        tracks = asyncio.run(provider.get_playlist_tracks(SUPERMIX_ID))

    assert tracks == []
    messages = [record.getMessage() for record in caplog.records]
    assert any("get_watch_playlist failed" in message for message in messages), (
        f"the failure was discarded silently; log held: {messages!r}"
    )
    assert any(SUPERMIX_ID in message for message in messages)


async def _empty_fallback(_playlist_id, _seed=None):
    return []


def test_a_mix_yields_nothing_beyond_the_first_page(provider):
    """Pin the cap RADIO_PLAYLIST_LIMIT documents.

    The radio endpoint has no offset, so there is no honest second page: this
    returns empty rather than refetching and slicing a sequence that is not
    stable across calls.
    """
    mock = MagicMock()
    mock.get_watch_playlist = MagicMock(
        side_effect=AssertionError("page 1 must not reach the network")
    )
    provider._ytmusic = mock

    assert asyncio.run(provider.get_playlist_tracks(SUPERMIX_ID, page=1)) == []


# ---------------------------------------------------------------------------
# get_playlist (metadata) routing
#
# The track path above was fixed first, and on its own it left a mix half
# working: MA resolves a playlist's details as well as its tracks, and details
# still went out as playlist?list=RD..., which YouTube refuses. So "My
# Supermix" could still fail to open with its tracks sitting there reachable.
# ---------------------------------------------------------------------------


class _RecordingYDL:
    """Stands in for yt_dlp.YoutubeDL and records the URLs it is handed."""

    urls: list[str] = []

    def __init__(self, opts):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def extract_info(self, url, download=False):
        type(self).urls.append(url)
        if "/playlist?list=RD" in url:
            # What YouTube really answers for a radio id in the plain form.
            raise RuntimeError("This playlist type is unviewable")
        return {"title": "My Supermix", "uploader": "YouTube Music", "thumbnails": []}


@pytest.fixture
def ydl(provider):
    """Give the provider a recording yt-dlp and a ytmusicapi that fails on mixes."""
    import types

    _RecordingYDL.urls = []
    module = types.ModuleType("yt_dlp")
    module.YoutubeDL = _RecordingYDL
    provider._yt_dlp_module = module

    mock = MagicMock()
    mock.get_playlist = MagicMock(side_effect=KeyError("contents"))
    mock.get_watch_playlist = MagicMock(return_value={"tracks": [_watch_track("seedvideoid")]})
    provider._ytmusic = mock
    return provider


def test_curated_mix_details_resolve_via_a_seed_from_the_watch_endpoint(ydl):
    """The regression: this raised MediaNotFoundError for every curated mix."""
    playlist = asyncio.run(ydl.get_playlist(SUPERMIX_ID))

    assert playlist.name == "My Supermix"
    assert playlist.item_id == SUPERMIX_ID
    assert _RecordingYDL.urls == [
        f"https://www.youtube.com/watch?v=seedvideoid&list={SUPERMIX_ID}"
    ]


@pytest.mark.parametrize("playlist_id", [SUPERMIX_ID, EDITORIAL_ID, f"VL{SUPERMIX_ID}"])
def test_no_mix_asks_for_details_in_the_unviewable_form(ydl, playlist_id):
    """Guard the root cause, not one example of it."""
    asyncio.run(ydl.get_playlist(playlist_id))

    assert _RecordingYDL.urls, "no request was made at all"
    for url in _RecordingYDL.urls:
        assert "/playlist?list=" not in url, f"{playlist_id} would be unviewable at {url}"


def test_the_seed_lookup_asks_for_a_single_track(ydl):
    """A seed needs one track; fetching a queue's worth here is wasted work."""
    asyncio.run(ydl.get_playlist(SUPERMIX_ID))

    kwargs = ydl._ytmusic.get_watch_playlist.call_args.kwargs
    assert kwargs["playlistId"] == SUPERMIX_ID
    assert kwargs["limit"] == 1


def test_song_radio_details_need_no_extra_lookup(ydl):
    """Its seed is in the id already, so spending a request on one is waste."""
    playlist = asyncio.run(ydl.get_playlist(SONG_RADIO_ID))

    assert playlist.name == "My Supermix"
    assert _RecordingYDL.urls == [
        f"https://www.youtube.com/watch?v=dQw4w9WgXcQ&list={SONG_RADIO_ID}"
    ]
    ydl._ytmusic.get_watch_playlist.assert_not_called()


def test_static_playlist_details_never_touch_the_watch_endpoint(ydl):
    """Ordinary playlists keep the plain form and cost no extra request."""
    ydl._ytmusic.get_watch_playlist = MagicMock(
        side_effect=AssertionError("watch endpoint must not be used for PL ids")
    )

    asyncio.run(ydl.get_playlist(STATIC_ID))

    assert _RecordingYDL.urls == [f"https://www.youtube.com/playlist?list={STATIC_ID}"]


def test_working_ytmusicapi_details_are_left_alone(provider):
    """The fallback must not fire when the ordinary path already answered."""
    mock = MagicMock()
    mock.get_playlist = MagicMock(
        return_value={"id": STATIC_ID, "title": "Real Playlist", "tracks": []}
    )
    mock.get_watch_playlist = MagicMock(side_effect=AssertionError("no fallback expected"))
    provider._ytmusic = mock

    assert asyncio.run(provider.get_playlist(STATIC_ID)).name == "Real Playlist"


def test_a_seedless_mix_still_reports_not_found(ydl, caplog):
    """When the endpoint yields no seed there is nothing left to try.

    It must still fail loudly rather than quietly: a silent dead end is what
    made the original report impossible to act on.
    """
    ydl._ytmusic.get_watch_playlist = MagicMock(return_value={"tracks": []})

    with caplog.at_level("WARNING"), pytest.raises(ytm.MediaNotFoundError, match="not found"):
        asyncio.run(ydl.get_playlist(SUPERMIX_ID))

    joined = " ".join(record.getMessage() for record in caplog.records)
    assert "no seed track" in joined, f"the dead end was silent; log held: {joined!r}"


def test_a_failing_seed_lookup_is_logged_not_swallowed(ydl, caplog):
    ydl._ytmusic.get_watch_playlist = MagicMock(side_effect=KeyError("endpoint"))

    with caplog.at_level("WARNING"), pytest.raises(ytm.MediaNotFoundError, match="not found"):
        asyncio.run(ydl.get_playlist(SUPERMIX_ID))

    joined = " ".join(record.getMessage() for record in caplog.records)
    assert "seed track" in joined, f"the failure was discarded; log held: {joined!r}"
    assert SUPERMIX_ID in joined


def test_similar_tracks_failure_is_logged_at_warning(provider, caplog):
    """Same endpoint and same failure as the mix path, so same visibility.

    At debug level this said nothing at all in a default install, which is the
    blind spot that let issue #47 sit unexplained for months.
    """
    mock = MagicMock()
    mock.get_watch_playlist = MagicMock(side_effect=KeyError("endpoint"))
    provider._ytmusic = mock

    with caplog.at_level("WARNING"):
        assert asyncio.run(provider.get_similar_tracks("dQw4w9WgXcQ")) == []

    joined = " ".join(record.getMessage() for record in caplog.records)
    assert "get_watch_playlist failed" in joined, f"nothing logged; held: {joined!r}"
    assert "dQw4w9WgXcQ" in joined


def test_ytdlp_extraction_failure_is_logged(provider, caplog):
    """Both swallow sites used to discard the error entirely."""

    class _Boom:
        def __init__(self, opts):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def extract_info(self, url, download=False):
            raise RuntimeError("This playlist type is unviewable")

    import types

    module = types.ModuleType("yt_dlp")
    module.YoutubeDL = _Boom
    provider._yt_dlp_module = module

    with caplog.at_level("WARNING"):
        tracks = asyncio.run(provider._get_playlist_tracks_via_ytdlp(STATIC_ID))

    assert tracks == []
    joined = " ".join(record.getMessage() for record in caplog.records)
    assert "unviewable" in joined, f"error was discarded; log held: {joined!r}"
    assert STATIC_ID in joined
