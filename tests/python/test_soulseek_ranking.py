"""Duration-first matching tests for the Soulseek provider."""

from __future__ import annotations

import pytest

from soulseek.ranking import (
    PeerFile,
    deduplicate_variants,
    diversify_peers,
    rank_variants,
)


def file(
    user: str,
    name: str,
    *,
    duration: float | None,
    extension: str = "mp3",
    bitrate: int = 320,
    free: bool | None = None,
    queue: int | None = None,
    speed: int | None = None,
) -> PeerFile:
    return PeerFile(
        user,
        rf"Music\{name}.{extension}",
        1000,
        extension,
        bitrate,
        duration,
        free_upload_slots=free,
        queue_size=queue,
        upload_speed=speed,
    )


def test_catalog_duration_rejects_remix_outlier_and_keeps_two_second_band():
    variants = [
        file("a", "Artist - Song", duration=200),
        file("b", "Artist - Song clean", duration=202, extension="flac", bitrate=900),
        file("c", "Artist - Song remix", duration=260, extension="flac", bitrate=1000),
    ]
    ranked = rank_variants(
        variants, artist="Artist", title="Song", catalog_duration=200, prefer_flac=True
    )
    assert [item.username for item in ranked] == ["b", "a"]


def test_duration_is_considered_before_format_preference():
    variants = [
        file("exact", "Artist Song", duration=180, extension="mp3"),
        file("wrong", "Artist Song extended", duration=230, extension="flac"),
    ]
    assert (
        rank_variants(
            variants,
            artist="Artist",
            title="Song",
            catalog_duration=180,
            prefer_flac=True,
        )[0].username
        == "exact"
    )


def test_format_preference_and_fallback_inside_duration_group():
    variants = [
        file("mp3", "Artist Song", duration=180, extension="mp3", bitrate=320),
        file("flac", "Artist Song", duration=180, extension="flac", bitrate=900),
    ]
    assert (
        rank_variants(
            variants,
            artist="Artist",
            title="Song",
            catalog_duration=180,
            prefer_flac=True,
        )[0].username
        == "flac"
    )
    assert (
        rank_variants(
            variants,
            artist="Artist",
            title="Song",
            catalog_duration=180,
            prefer_flac=False,
        )[0].username
        == "mp3"
    )


def test_missing_catalog_duration_uses_densest_two_second_cluster():
    variants = [
        file("a", "Song", duration=199),
        file("b", "Song", duration=200),
        file("c", "Song", duration=201),
        file("d", "Song remix", duration=260),
        file("e", "Song remix", duration=261),
    ]
    ranked = rank_variants(
        variants,
        artist="Artist",
        title="Song",
        catalog_duration=None,
        prefer_flac=False,
    )
    assert {item.username for item in ranked} == {"a", "b", "c"}
    assert ranked[0].username == "b"


def test_equal_clusters_break_by_distinct_peers_then_spread():
    variants = [
        file("same", "Song A", duration=100),
        file("same", "Song B", duration=102),
        file("p1", "Song", duration=200),
        file("p2", "Song", duration=201),
    ]
    ranked = rank_variants(
        variants,
        artist="Artist",
        title="Song",
        catalog_duration=None,
        prefer_flac=False,
    )
    assert {item.username for item in ranked} == {"p1", "p2"}


def test_no_reported_durations_falls_back_to_format_relevance_bitrate():
    variants = [
        file("weak", "unrelated", duration=None, bitrate=320),
        file("match", "Artist Song", duration=None, bitrate=192),
    ]
    assert (
        rank_variants(
            variants,
            artist="Artist",
            title="Song",
            catalog_duration=None,
            prefer_flac=False,
        )[0].username
        == "match"
    )


def test_deduplication_and_deterministic_ties():
    duplicate = file("Peer", "Artist Song", duration=180)
    variants = [duplicate, duplicate, file("alpha", "Artist Song", duration=180)]
    assert len(deduplicate_variants(variants)) == 2
    first = rank_variants(
        variants, artist="Artist", title="Song", catalog_duration=180, prefer_flac=False
    )
    second = rank_variants(
        reversed(variants),
        artist="Artist",
        title="Song",
        catalog_duration=180,
        prefer_flac=False,
    )
    assert [(item.username, item.path) for item in first] == [
        (item.username, item.path) for item in second
    ]


def test_non_audio_results_fail_clearly():
    with pytest.raises(ValueError, match="No FLAC or MP3"):
        rank_variants(
            [PeerFile("peer", "song.wav", extension="wav")],
            artist="Artist",
            title="Song",
            catalog_duration=180,
            prefer_flac=False,
        )


def test_filename_relevance_precedes_peer_speed():
    variants = [
        file("fast-wrong", "unrelated", duration=180, free=True, speed=10_000),
        file("right", "Artist Song", duration=180, free=True, speed=100),
    ]
    ranked = rank_variants(
        variants, artist="Artist", title="Song", catalog_duration=180, prefer_flac=False
    )
    assert ranked[0].username == "right"


def test_free_slot_then_queue_then_upload_speed_rank_equivalent_files():
    variants = [
        file("queued-fast", "Artist Song", duration=180, free=False, queue=0, speed=50_000),
        file("free-queued", "Artist Song", duration=180, free=True, queue=4, speed=50_000),
        file("free-slow", "Artist Song", duration=180, free=True, queue=0, speed=100),
        file("free-fast", "Artist Song", duration=180, free=True, queue=0, speed=1_000),
    ]
    ranked = rank_variants(
        variants, artist="Artist", title="Song", catalog_duration=180, prefer_flac=False
    )
    assert [item.username for item in ranked] == [
        "free-fast",
        "free-slow",
        "free-queued",
        "queued-fast",
    ]


def test_missing_peer_metrics_are_valid_but_known_free_peer_wins():
    variants = [
        file("unknown", "Artist Song", duration=180),
        file("busy", "Artist Song", duration=180, free=False, queue=0, speed=10000),
        file("available", "Artist Song", duration=180, free=True, queue=1, speed=100),
    ]
    ranked = rank_variants(
        variants, artist="Artist", title="Song", catalog_duration=180, prefer_flac=False
    )
    assert [item.username for item in ranked] == ["available", "unknown", "busy"]


def test_failover_candidates_prefer_distinct_peers():
    ranked = [
        file("same", "Artist Song one", duration=180),
        file("same", "Artist Song two", duration=180),
        file("other", "Artist Song", duration=180),
        file("third", "Artist Song", duration=180),
    ]
    assert [item.username for item in diversify_peers(ranked, 3)] == [
        "same",
        "other",
        "third",
    ]
