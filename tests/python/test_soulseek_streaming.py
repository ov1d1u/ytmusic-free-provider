"""Memory-only progressive playback, failover, and cleanup."""

from __future__ import annotations

import asyncio
from dataclasses import asdict
from types import SimpleNamespace

import pytest
from music_assistant_models.enums import MediaType, StreamType
from music_assistant_models.errors import UnplayableMediaError
from music_assistant_models.streamdetails import StreamDetails

import soulseek
from soulseek.client import DownloadHandle
from soulseek.memory import AudioBuffer
from soulseek.musicbrainz import MusicBrainzItem

from soulseek.ranking import PeerFile

TRACK = MusicBrainzItem(kind="track", item_id="recording-30", name="Song",
                       artist_name="Artist", artist_id="10", duration=180)


class FakeClient:
    def __init__(self, handles):
        self.handles = handles
        self.started = 0
        self.aborted = 0

    async def search_variants(self, query):
        assert query == "Artist Song"
        return [handle.peer_file for handle in self.handles]

    async def start_download(self, peer_file):
        result = self.handles[self.started]
        self.started += 1
        return result

    async def abort_download(self, handle):
        self.aborted += 1
        handle.buffer.release()


def make_provider(handles):
    provider = soulseek.SoulseekProvider(mass=None, manifest=None, config=None)
    provider._quality = "mp3"
    provider._client = FakeClient(handles)
    return provider


def streamdetails():
    return StreamDetails(item_id=TRACK.item_id, data={"track": asdict(TRACK)})


def handle(peer, audio=b"", *, finished=False, failed=False):
    result = DownloadHandle(
        peer_file=PeerFile(peer, f"Artist - Song {peer}.mp3", 100, "mp3", 320, 180),
        finished=finished, failed=failed,
        error="Connection timeout" if failed else "",
    )
    result.buffer = AudioBuffer(result.updated.set)
    result.buffer.write(audio)
    return result


async def collect(generator):
    return b"".join([chunk async for chunk in generator])


def test_first_byte_failover_and_exact_audio():
    audio = b"audio" * 20000 + b"end"
    provider = make_provider([handle("one", failed=True), handle("two", failed=True),
                              handle("three", audio, finished=True)])
    assert asyncio.run(collect(provider.get_audio_stream(streamdetails()))) == audio
    assert provider._client.started == 3
    assert provider._client.aborted == 3


def test_audio_is_available_before_transfer_finishes_and_stop_releases_memory(tmp_path):
    async def run():
        incoming = handle("one")
        provider = make_provider([incoming])
        generator = provider.get_audio_stream(streamdetails())
        pending = asyncio.create_task(anext(generator))
        await asyncio.sleep(0)
        incoming.buffer.write(b"first")
        assert await asyncio.wait_for(pending, 1) == b"first"
        assert not incoming.finished
        incoming.buffer.write(b"second")
        assert await anext(generator) == b"second"
        await generator.aclose()
        assert provider._client.aborted == 1
        assert len(incoming.buffer) == 0
        assert list(tmp_path.iterdir()) == []
    asyncio.run(run())


def test_midstream_stall_never_switches_variant_and_aborts(monkeypatch):
    provider = make_provider([handle("one", b"first"), handle("two", b"other", finished=True)])
    monkeypatch.setattr(soulseek, "MIDSTREAM_STALL_TIMEOUT", 0.01)

    async def run():
        generator = provider.get_audio_stream(streamdetails())
        assert await anext(generator) == b"first"
        with pytest.raises(UnplayableMediaError, match="after playback began"):
            await anext(generator)
    asyncio.run(run())
    assert provider._client.started == 1
    assert provider._client.aborted == 1


def test_cancelling_before_first_bytes_aborts():
    async def run():
        provider = make_provider([handle("one")])
        pending = asyncio.create_task(collect(provider.get_audio_stream(streamdetails())))
        await asyncio.sleep(0)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert provider._client.aborted == 1
    asyncio.run(run())


def test_empty_finished_transfer_fails_over_immediately():
    provider = make_provider([handle("one", finished=True), handle("two", b"audio", finished=True)])
    assert asyncio.run(collect(provider.get_audio_stream(streamdetails()))) == b"audio"
    assert provider._client.aborted == 2


def test_stream_details_always_use_custom_stream():
    async def track_page(item_id):
        return TRACK
    provider = make_provider([])
    provider._catalog = SimpleNamespace(track_page=track_page)
    details = asyncio.run(provider.get_stream_details(TRACK.item_id, MediaType.TRACK))
    assert details.stream_type == StreamType.CUSTOM
    assert not details.path and not details.can_seek and not details.allow_seek


def test_memory_sink_limit_and_completion():
    sink = AudioBuffer(lambda: None, limit=8)
    sink.write(b"12345678")
    with pytest.raises(OSError, match="memory limit"):
        sink.write(b"9")
    sink.close()
    assert sink.read(0, 8) == b"12345678"
    with pytest.raises(OSError, match="closed"):
        sink.write(b"9")
    sink.release()
    assert len(sink) == 0
