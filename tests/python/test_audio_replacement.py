"""Official-audio selection for search, mixes and song radio."""

from __future__ import annotations

import asyncio
import threading
import time
from unittest.mock import MagicMock

import pytest

from music_assistant_models.enums import MediaType

import ytmusic_free as ytm


OMV = "MUSIC_VIDEO_TYPE_OMV"
UGC = "MUSIC_VIDEO_TYPE_UGC"


def _track(
    video_id: str = "video000001",
    *,
    title: str = "Café Song (Official Music Video)",
    artist: str = "The Artist",
    artist_id: str | None = "UCartist",
    video_type: str | None = OMV,
    **extra,
):
    result = {
        "videoId": video_id,
        "title": title,
        "artists": [{"name": artist, "id": artist_id}],
    }
    if video_type is not None:
        result["videoType"] = video_type
    result.update(extra)
    return result


def _audio(
    video_id: str = "audio000001",
    *,
    title: str = "Cafe Song",
    artist: str = "The Artist",
    artist_id: str | None = "UCartist",
    video_type: str | None = ytm.VIDEO_TYPE_ATV,
    **extra,
):
    result = {
        "videoId": video_id,
        "title": title,
        "artists": [{"name": artist, "id": artist_id}],
        "resultType": "song",
    }
    if video_type is not None:
        result["videoType"] = video_type
    result.update(extra)
    return result


def test_title_matching_removes_only_video_labels():
    cleaned = ytm._audio_song_title("Café Song (Live) [Official Music Video]")
    assert cleaned == "Café Song (Live)"
    assert ytm._audio_text_key(cleaned) == ytm._audio_text_key("CAFE-SONG: LIVE")
    assert ytm._audio_song_title("Song (Remix)") == "Song (Remix)"
    assert ytm._audio_song_title("Song (Acoustic)") == "Song (Acoustic)"


def test_typed_atv_is_kept_without_any_api_lookup(provider):
    client = MagicMock()
    provider._ytmusic = client
    original = _audio(duration="9:59", album={"id": "album", "name": "Album"})

    resolved = asyncio.run(provider._resolve_preferred_audio(original))

    assert resolved == original
    assert resolved is not original
    client.get_watch_playlist.assert_not_called()
    client.search.assert_not_called()


def test_missing_original_type_uses_only_the_exact_queue_entry(provider):
    client = MagicMock()
    original = _track(video_type=None)
    exact = _audio(original["videoId"], title=original["title"])
    client.get_watch_playlist.return_value = {
        "tracks": [_audio("unrelated01"), exact]
    }
    provider._ytmusic = client

    resolved = asyncio.run(provider._resolve_preferred_audio(original))

    assert resolved["videoId"] == original["videoId"]
    assert resolved["videoType"] == ytm.VIDEO_TYPE_ATV
    client.search.assert_not_called()


def test_unrelated_first_queue_entry_does_not_verify_the_candidate(provider):
    client = MagicMock()
    original = _track(video_type=None)
    client.get_watch_playlist.return_value = {"tracks": [_audio("unrelated01")]}
    client.search.return_value = []
    provider._ytmusic = client

    assert asyncio.run(provider._resolve_preferred_audio(original)) is None


def test_typed_and_untyped_atv_counterparts_are_preferred(provider):
    typed = _audio("counterpart1", duration="2:00")
    client = MagicMock()
    provider._ytmusic = client
    resolved = asyncio.run(
        provider._resolve_preferred_audio(_track(counterpart=typed))
    )
    assert resolved["videoId"] == "counterpart1"
    client.search.assert_not_called()
    client.get_watch_playlist.assert_not_called()

    provider._audio_replacement_cache.clear()
    untyped = _audio("counterpart2", video_type=None)
    verified = _audio("counterpart2", length="3:01")
    client.get_watch_playlist.return_value = {"tracks": [verified]}
    resolved = asyncio.run(
        provider._resolve_preferred_audio(
            _track("video000002", counterpart=untyped)
        )
    )
    assert resolved["videoId"] == "counterpart2"
    assert resolved["videoType"] == ytm.VIDEO_TYPE_ATV


@pytest.mark.parametrize("video_type", [OMV, UGC, "MUSIC_VIDEO_TYPE_UNKNOWN"])
def test_all_non_atv_types_search_by_clean_title_and_artist(provider, video_type):
    client = MagicMock()
    client.search.return_value = [
        _audio(
            title="Cafe Song (Live)",
            artist="THE ARTIST",
            artist_id=None,
            duration="0:42",
        )
    ]
    provider._ytmusic = client
    original = _track(
        title="Café Song (Live) (Official Music Video)",
        video_type=video_type,
        duration=9999,
        album={"id": "video-album", "name": "Wrong Album"},
    )

    resolved = asyncio.run(provider._resolve_preferred_audio(original))

    assert resolved["videoId"] == "audio000001"
    assert resolved["duration"] == "0:42"  # Duration is output metadata, not a match key.
    assert "album" not in resolved
    client.search.assert_called_once_with(
        "The Artist Café Song (Live)",
        filter="songs",
        limit=ytm.AUDIO_REPLACEMENT_SEARCH_LIMIT,
    )


def test_artist_id_can_match_when_the_display_names_differ(provider):
    client = MagicMock()
    client.search.return_value = [_audio(artist="Localized Artist")]
    provider._ytmusic = client

    resolved = asyncio.run(provider._resolve_preferred_audio(_track()))

    assert resolved["videoId"] == "audio000001"


def test_search_rejects_bad_candidates_and_verifies_a_missing_type(provider):
    original = _track()
    untyped = _audio("rightaudio1", video_type=None)
    client = MagicMock()
    client.search.return_value = [
        _audio(original["videoId"]),
        _audio("unavailable", isAvailable=False),
        _audio("wrongtitle1", title="Cafe Song (Remix)"),
        _audio("wrongartist", artist="Cover Band", artist_id="UCcover"),
        _audio("musicvideo1", video_type=OMV),
        untyped,
    ]
    client.get_watch_playlist.return_value = {
        "tracks": [_audio("another-id"), _audio("rightaudio1")]
    }
    provider._ytmusic = client

    resolved = asyncio.run(provider._resolve_preferred_audio(original))

    assert resolved["videoId"] == "rightaudio1"
    client.get_watch_playlist.assert_called_once_with(videoId="rightaudio1", limit=1)


def test_only_five_matching_candidates_are_examined(provider):
    client = MagicMock()
    client.search.return_value = [
        _audio(f"candidate{i}", video_type=None) for i in range(8)
    ]
    client.get_watch_playlist.return_value = {"tracks": []}
    provider._ytmusic = client

    assert asyncio.run(provider._resolve_preferred_audio(_track())) is None
    assert client.get_watch_playlist.call_count == ytm.AUDIO_REPLACEMENT_VERIFY_LIMIT


def test_positive_and_confirmed_negative_resolutions_are_cached(provider):
    client = MagicMock()
    client.search.return_value = [_audio()]
    provider._ytmusic = client
    original = _track()

    first = asyncio.run(provider._resolve_preferred_audio(original))
    second = asyncio.run(provider._resolve_preferred_audio(original))
    assert first == second
    assert client.search.call_count == 1

    provider._audio_replacement_cache.clear()
    client.search.reset_mock()
    client.search.return_value = []
    assert asyncio.run(provider._resolve_preferred_audio(original)) is None
    assert asyncio.run(provider._resolve_preferred_audio(original)) is None
    assert client.search.call_count == 1


def test_resolution_cache_expires_after_one_day(provider):
    client = MagicMock()
    client.search.side_effect = [
        [_audio("audio000001")],
        [_audio("audio000002")],
    ]
    provider._ytmusic = client
    original = _track()

    assert asyncio.run(provider._resolve_preferred_audio(original))["videoId"] == "audio000001"
    _, value = provider._audio_replacement_cache[original["videoId"]]
    provider._audio_replacement_cache[original["videoId"]] = (
        time.monotonic() - ytm.AUDIO_REPLACEMENT_CACHE_TTL - 1,
        value,
    )

    assert asyncio.run(provider._resolve_preferred_audio(original))["videoId"] == "audio000002"
    assert client.search.call_count == 2


def test_resolution_cache_has_a_hard_lru_size_limit(provider):
    async def _initialize():
        provider._ensure_audio_replacement_state()

    asyncio.run(_initialize())
    for index in range(ytm.AUDIO_REPLACEMENT_CACHE_SIZE + 1):
        provider._cache_audio_replacement(
            f"video{index}", _audio(f"audio{index}")
        )

    assert len(provider._audio_replacement_cache) == ytm.AUDIO_REPLACEMENT_CACHE_SIZE
    assert "video0" not in provider._audio_replacement_cache
    assert f"video{ytm.AUDIO_REPLACEMENT_CACHE_SIZE}" in provider._audio_replacement_cache


def test_api_errors_are_not_negative_cached(provider):
    client = MagicMock()
    client.search.side_effect = [RuntimeError("temporary"), [_audio()]]
    provider._ytmusic = client
    original = _track()

    assert asyncio.run(provider._resolve_preferred_audio(original)) is None
    assert asyncio.run(provider._resolve_preferred_audio(original))["videoId"] == "audio000001"
    assert client.search.call_count == 2


def test_duplicate_inflight_resolution_is_coalesced(provider):
    client = MagicMock()
    release = threading.Event()

    def _search(*args, **kwargs):
        release.wait(timeout=1)
        return [_audio()]

    client.search.side_effect = _search
    provider._ytmusic = client

    async def _run():
        first = asyncio.create_task(provider._resolve_preferred_audio(_track()))
        second = asyncio.create_task(provider._resolve_preferred_audio(_track()))
        await asyncio.sleep(0)
        release.set()
        return await asyncio.gather(first, second)

    results = asyncio.run(_run())
    assert [item["videoId"] for item in results] == ["audio000001", "audio000001"]
    assert client.search.call_count == 1


def test_resolution_concurrency_is_bounded(provider):
    client = MagicMock()
    active = 0
    maximum = 0
    lock = threading.Lock()

    def _search(*args, **kwargs):
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
        time.sleep(0.03)
        with lock:
            active -= 1
        return []

    client.search.side_effect = _search
    provider._ytmusic = client
    items = [
        ("watch", _track(f"video{i:06d}"))
        for i in range(12)
    ]

    assert asyncio.run(provider._resolve_preferred_audio_batch(items)) == []
    assert maximum == ytm.AUDIO_REPLACEMENT_CONCURRENCY


def test_batch_deduplicates_after_two_sources_resolve_to_one_atv(provider):
    client = MagicMock()
    client.search.return_value = [_audio()]
    provider._ytmusic = client
    items = [
        ("songs", _track("video000001")),
        ("videos", _track("video000002", video_type=UGC)),
    ]

    resolved = asyncio.run(provider._resolve_preferred_audio_batch(items))

    assert [(source, item["videoId"]) for source, item in resolved] == [
        ("songs", "audio000001")
    ]


def test_text_search_replaces_then_deduplicates_song_and_video_results(provider):
    client = MagicMock()

    def _search(query, filter, limit):  # noqa: A002
        if query == "wanted":
            if filter == "songs":
                return [{**_track("video000001"), "resultType": "song"}]
            if filter == "videos":
                return [{**_track("video000002", video_type=UGC), "resultType": "video"}]
        return [_audio(duration="1:02")]

    client.search.side_effect = _search
    provider._ytmusic = client

    results = asyncio.run(provider.search("wanted", [MediaType.TRACK], limit=5))

    assert [track.item_id for track in results.tracks] == ["audio000001"]
    assert results.tracks[0].duration == 62


def test_unmatched_upload_and_podcast_episode_do_not_become_music_results(provider):
    client = MagicMock()

    def _search(query, filter, limit):  # noqa: A002
        if query == "wanted" and filter == "videos":
            return [
                {
                    **_track("episode001", video_type=ytm.VIDEO_TYPE_PODCAST_EPISODE),
                    "resultType": "video",
                },
                {**_track("upload0001", video_type=UGC), "resultType": "video"},
            ]
        return []

    client.search.side_effect = _search
    provider._ytmusic = client

    results = asyncio.run(provider.search("wanted", [MediaType.TRACK]))

    assert results.tracks == []
    client.get_watch_playlist.assert_not_called()


def test_mix_replacement_deduplicates_and_reindexes_positions(provider):
    counterpart = _audio("audio000001", length="1:01")
    client = MagicMock()
    client.get_watch_playlist.return_value = {
        "tracks": [
            _track("video000001", counterpart=counterpart),
            _track("video000002", video_type=UGC),
            _track("video000003", title="No Match", artist="Nobody"),
        ]
    }

    def _search(query, filter, limit):  # noqa: A002
        return [_audio()] if query.startswith("The Artist") else []

    client.search.side_effect = _search
    provider._ytmusic = client

    tracks = asyncio.run(provider._get_radio_playlist_tracks("RDmix"))

    assert [track.item_id for track in tracks] == ["audio000001"]
    assert [track.position for track in tracks] == [1]
    assert tracks[0].duration == 61


def test_mix_with_only_unmatched_items_does_not_reintroduce_them_via_fallback(provider):
    client = MagicMock()
    client.get_watch_playlist.return_value = {
        "tracks": [_track("video000001", title="No Album Audio")]
    }
    client.search.return_value = []
    client.get_playlist.side_effect = AssertionError("successful watch result is authoritative")
    provider._ytmusic = client
    provider._get_playlist_tracks_via_ytdlp = MagicMock(
        side_effect=AssertionError("yt-dlp must not reintroduce the video")
    )

    tracks = asyncio.run(provider.get_playlist_tracks("RDvideo000001"))

    assert tracks == []
    client.get_playlist.assert_not_called()
    provider._get_playlist_tracks_via_ytdlp.assert_not_called()


def test_similar_tracks_use_replacement_metadata(provider):
    client = MagicMock()
    client.get_watch_playlist.return_value = {
        "tracks": [_track("video000001", video_type=UGC, duration=9999)]
    }
    client.search.return_value = [
        _audio(
            duration="0:42",
            album={"id": "audio-album", "name": "Audio Album"},
        )
    ]
    provider._ytmusic = client

    tracks = asyncio.run(provider.get_similar_tracks("seed0000001"))

    assert [track.item_id for track in tracks] == ["audio000001"]
    assert tracks[0].duration == 42
    assert tracks[0].album.item_id == "audio-album"


def test_ordinary_playlist_does_not_replace_music_videos(provider):
    client = MagicMock()
    client.get_playlist.return_value = {
        "tracks": [_track("video000001", video_type=OMV)]
    }
    provider._ytmusic = client

    tracks = asyncio.run(provider.get_playlist_tracks("PLordinary"))

    assert [track.item_id for track in tracks] == ["video000001"]
    client.search.assert_not_called()
    client.get_watch_playlist.assert_not_called()


def test_featured_editorial_playlist_replaces_drops_and_reindexes(provider):
    first_audio = _audio(
        "audio000001",
        duration="1:01",
        album={"id": "album-audio", "name": "Audio Album"},
    )
    second_audio = _audio(
        "audio000002",
        title="Second Song",
        duration="0:42",
    )
    client = MagicMock()
    client.get_playlist.return_value = {
        "trackCount": 4,
        "tracks": [
            _track("video000001", counterpart=first_audio),
            _track(
                "video000002",
                title="Second Song (Official Music Video)",
                video_type=UGC,
            ),
            _track("video000003", title="No Matching Audio", artist="Nobody"),
            _track("video000004", counterpart=first_audio),
        ],
    }

    def _search(query, filter, limit):  # noqa: A002
        return [second_audio] if query.startswith("The Artist Second Song") else []

    client.search.side_effect = _search
    provider._ytmusic = client

    tracks = asyncio.run(
        provider.get_playlist_tracks(
            "RDCLAK5uy_k6PkYWus1Mt-aKrbb0Ne8SkA2BgAk1Yy4"
        )
    )

    assert [track.item_id for track in tracks] == ["audio000001", "audio000002"]
    assert [track.position for track in tracks] == [1, 2]
    assert [track.duration for track in tracks] == [61, 42]
    assert tracks[0].album.item_id == "album-audio"
    client.get_playlist.assert_called_once()


def test_featured_editorial_playlist_keeps_full_playlist_endpoint(provider):
    client = MagicMock()
    client.get_playlist.return_value = {
        "trackCount": 2,
        "tracks": [_audio("audio000001"), _audio("audio000002")],
    }
    provider._ytmusic = client

    tracks = asyncio.run(
        provider.get_playlist_tracks(
            "RDCLAK5uy_k6PkYWus1Mt-aKrbb0Ne8SkA2BgAk1Yy4"
        )
    )

    assert [track.item_id for track in tracks] == ["audio000001", "audio000002"]
    client.get_playlist.assert_called_once_with(
        "RDCLAK5uy_k6PkYWus1Mt-aKrbb0Ne8SkA2BgAk1Yy4", limit=None
    )
    client.get_watch_playlist.assert_not_called()
