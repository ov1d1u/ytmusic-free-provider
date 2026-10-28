"""Progressive stream and persistent-cache behavior."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from music_assistant_models.enums import MediaType, StreamType
from music_assistant_models.errors import UnplayableMediaError
from music_assistant_models.streamdetails import StreamDetails

import soulseek
from soulseek.cache import DownloadCache
from soulseek.client import DownloadHandle
from soulseek.musicbrainz import MusicBrainzItem
from soulseek.ranking import PeerFile

TRACK = MusicBrainzItem(
    kind="track",
    item_id="recording-30",
    name="Song",
    artist_name="Artist",
    artist_id="10",
    duration=180,
)


class FakeClient:
    def __init__(self, handles: list[DownloadHandle]):
        self.handles = handles
        self.started = 0
        self.aborted = 0

    async def search_variants(self, query: str):
        assert query == "Artist Song"
        return [handle.peer_file for handle in self.handles]

    async def start_download(self, peer_file, *, folder, context):
        del peer_file, folder, context
        handle = self.handles[self.started]
        self.started += 1
        return handle

    async def abort_download(self, handle):
        del handle
        self.aborted += 1


def make_provider(tmp_path: Path, handles: list[DownloadHandle]):
    provider = soulseek.SoulseekProvider(mass=None, manifest=None, config=None)
    provider._quality = "mp3"
    provider._cache = DownloadCache(tmp_path / "soulseek")
    asyncio.run(provider._cache.initialize())
    provider._client = FakeClient(handles)
    return provider


def streamdetails() -> StreamDetails:
    return StreamDetails(
        item_id="recording-30",
        data={
            "track": TRACK.__dict__
            if hasattr(TRACK, "__dict__")
            else {
                "kind": TRACK.kind,
                "item_id": TRACK.item_id,
                "name": TRACK.name,
                "url": TRACK.url,
                "artist_name": TRACK.artist_name,
                "artist_id": TRACK.artist_id,
                "album_name": TRACK.album_name,
                "album_id": TRACK.album_id,
                "artwork": TRACK.artwork,
                "duration": TRACK.duration,
                "year": TRACK.year,
                "explicit": TRACK.explicit,
                "track_number": TRACK.track_number,
                "disc_number": TRACK.disc_number,
            }
        },
    )


def handle(peer: str, path: Path | None, *, finished: bool, failed: bool = False):
    return DownloadHandle(
        peer_file=PeerFile(peer, f"Artist - Song {peer}.mp3", 100, "mp3", 320, 180),
        folder=path.parent if path else Path("."),
        context={},
        path=str(path) if path else "",
        finished=finished,
        failed=failed,
        error="Connection timeout" if failed else "",
    )


async def collect(generator):
    chunks = []
    async for chunk in generator:
        chunks.append(chunk)
    return b"".join(chunks)


def test_first_byte_failover_uses_at_most_three_peers_and_delivers_exact_bytes(
    tmp_path,
):
    audio = (b"audio" * 20000) + b"end"
    good_path = tmp_path / "good.mp3"
    good_path.write_bytes(audio)
    provider = make_provider(
        tmp_path,
        [
            handle("one", None, finished=False, failed=True),
            handle("two", None, finished=False, failed=True),
            handle("three", good_path, finished=True),
        ],
    )
    result = asyncio.run(collect(provider.get_audio_stream(streamdetails())))
    assert result == audio
    assert provider._client.started == 3
    assert provider._client.aborted == 2


def test_midstream_stall_never_switches_to_another_variant(tmp_path, monkeypatch):
    partial = tmp_path / "partial.mp3"
    partial.write_bytes(b"first bytes")
    waiting = handle("one", partial, finished=False)
    unused = handle("two", partial, finished=True)
    provider = make_provider(tmp_path, [waiting, unused])
    monkeypatch.setattr(soulseek, "MIDSTREAM_STALL_TIMEOUT", 0.05)

    async def run():
        generator = provider.get_audio_stream(streamdetails())
        assert await generator.__anext__() == b"first bytes"
        with pytest.raises(UnplayableMediaError, match="after playback began"):
            await generator.__anext__()

    asyncio.run(run())
    assert provider._client.started == 1
    assert provider._client.aborted == 0


def test_completed_cache_returns_seekable_local_file(tmp_path):
    provider = make_provider(tmp_path, [])
    cached_path = provider._cache.downloads_path / "cached.flac"
    cached_path.write_bytes(b"flac-data")
    provider._quality = "flac"
    asyncio.run(
        provider._cache.record(
            track_id="recording-30",
            profile="flac",
            path=cached_path,
            duration=180,
            bitrate=900,
        )
    )
    details = asyncio.run(provider.get_stream_details("recording-30", MediaType.TRACK))
    assert details.stream_type == StreamType.LOCAL_FILE
    assert details.path == str(cached_path)
    assert details.can_seek is True
    assert details.allow_seek is True


def test_startup_reconciles_completed_file_from_download_context(tmp_path):
    async def arrange():
        cache = DownloadCache(tmp_path / "soulseek")
        await cache.initialize()
        folder = await cache.prepare_download(
            track_id="recording-30", profile="mp3", duration=180, bitrate=320
        )
        completed = folder / "Artist - Song.mp3"
        completed.write_bytes(b"complete")
        restarted = DownloadCache(tmp_path / "soulseek")
        await restarted.initialize()
        return await restarted.lookup("recording-30", "mp3"), completed

    entry, completed = asyncio.run(arrange())
    assert entry is not None
    assert entry.path == str(completed)


def test_concurrent_consumers_each_receive_the_complete_file(tmp_path):
    audio = b"concurrent-audio"
    path = tmp_path / "complete.mp3"
    path.write_bytes(audio)
    provider = make_provider(
        tmp_path,
        [handle("one", path, finished=True), handle("two", path, finished=True)],
    )

    async def run():
        return await asyncio.gather(
            collect(provider.get_audio_stream(streamdetails())),
            collect(provider.get_audio_stream(streamdetails())),
        )

    assert asyncio.run(run()) == [audio, audio]


def test_cache_prefers_selected_format_but_reuses_fallback(tmp_path):
    async def run():
        cache = DownloadCache(tmp_path / "soulseek")
        await cache.initialize()
        mp3 = cache.downloads_path / "song.mp3"
        mp3.write_bytes(b"mp3")
        await cache.record(
            track_id="track", profile="mp3", path=mp3, duration=1, bitrate=320
        )
        return await cache.lookup("track", "flac")

    entry = asyncio.run(run())
    assert entry is not None
    assert entry.extension == "mp3"


def test_lru_cache_evicts_oldest_completed_file(tmp_path):
    async def run():
        cache = DownloadCache(tmp_path / "soulseek", limit_bytes=10)
        await cache.initialize()
        first = cache.downloads_path / "first.mp3"
        second = cache.downloads_path / "second.mp3"
        first.write_bytes(b"12345678")
        second.write_bytes(b"abcdefgh")
        await cache.record(track_id="first", profile="mp3", path=first, duration=1, bitrate=1)
        await cache.record(track_id="second", profile="mp3", path=second, duration=1, bitrate=1)
        return first, second, cache.debug_snapshot()

    first, second, snapshot = asyncio.run(run())
    assert not first.exists()
    assert second.exists()
    assert set(snapshot) == {"mp3:second"}


def test_lru_cache_never_evicts_active_track(tmp_path):
    async def run():
        cache = DownloadCache(tmp_path / "soulseek", limit_bytes=10)
        await cache.initialize()
        first = cache.downloads_path / "first.mp3"
        second = cache.downloads_path / "second.mp3"
        first.write_bytes(b"12345678")
        second.write_bytes(b"abcdefgh")
        await cache.record(track_id="first", profile="mp3", path=first, duration=1, bitrate=1)
        await cache.set_active("first", True)
        await cache.record(track_id="second", profile="mp3", path=second, duration=1, bitrate=1)
        await cache.set_active("first", False)
        return first, second, cache.debug_snapshot()

    first, second, snapshot = asyncio.run(run())
    assert first.exists()
    assert not second.exists()
    assert set(snapshot) == {"mp3:first"}


def test_cache_hit_updates_lru_access_time(tmp_path):
    async def run():
        cache = DownloadCache(tmp_path / "soulseek")
        await cache.initialize()
        path = cache.downloads_path / "song.mp3"
        path.write_bytes(b"audio")
        await cache.record(track_id="track", profile="mp3", path=path, duration=1, bitrate=1)
        before = cache.debug_snapshot()["mp3:track"]["last_accessed"]
        await asyncio.sleep(0.001)
        await cache.lookup("track", "mp3")
        return before, cache.debug_snapshot()["mp3:track"]["last_accessed"]

    before, after = asyncio.run(run())
    assert after > before


def test_startup_drops_missing_index_rows_and_migrates_access_time(tmp_path):
    async def run():
        cache = DownloadCache(tmp_path / "soulseek")
        await cache.initialize()
        present = cache.downloads_path / "present.mp3"
        present.write_bytes(b"audio")
        present.touch()
        cache.index_path.write_text(
            '{"mp3:present":{"track_id":"present","path":"'
            + str(present)
            + '","extension":"mp3","size":5,"profile":"mp3"},'
            '"mp3:missing":{"track_id":"missing","path":"'
            + str(cache.downloads_path / "missing.mp3")
            + '","extension":"mp3","size":5}}',
            encoding="utf-8",
        )
        restarted = DownloadCache(tmp_path / "soulseek")
        await restarted.initialize()
        return restarted.debug_snapshot()

    snapshot = asyncio.run(run())
    assert set(snapshot) == {"mp3:present"}
    assert snapshot["mp3:present"]["last_accessed"] > 0
