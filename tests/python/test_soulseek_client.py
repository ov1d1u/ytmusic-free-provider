"""Mocked Nicotine+ lifecycle and event-bridge tests."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from music_assistant_models.errors import SetupFailedError

from soulseek.cache import DownloadCache
from soulseek.client import DownloadHandle, SoulseekClient
from soulseek.ranking import PeerFile


class DummyEvents:
    def __init__(self):
        self.callbacks = {}

    def connect(self, name, callback):
        self.callbacks[name] = callback

    def process_thread_events(self):
        return None


class DummyConfig:
    def __init__(self):
        self.sections = {
            "server": {},
            "transfers": {},
            "searches": {},
        }
        self.writes = 0

    def set_data_folder(self, path):
        self.data_folder = path

    def set_config_file(self, path):
        self.config_file = path

    def write_configuration(self):
        self.writes += 1


class DummySearch:
    token = 0

    def do_search(self, query, mode):
        del query, mode
        self.token += 1

    def remove_search(self, token):
        del token

    @staticmethod
    def parse_audio_quality_length(size, attributes):
        del size, attributes
        return "320 kbps", 320, "3:00", 180


class DummyDownloads:
    def __init__(self):
        self.transfers = {}
        self.aborted = []
        self.cleared = []

    def abort_downloads(self, transfers, status):
        self.aborted.append((transfers, status))

    def clear_downloads(self, transfers):
        self.cleared.append(transfers)


class DummyCore:
    def __init__(self, events, *, login_success=True, reason=""):
        self.events = events
        self.login_success = login_success
        self.reason = reason
        self.search = DummySearch()
        self.downloads = DummyDownloads()
        self.started = self.connected = self.disconnected = self.quit_called = False

    def init_components(self, components, isolated_mode):
        self.components = components
        self.isolated_mode = isolated_mode

    def start(self):
        self.started = True

    def connect(self):
        self.connected = True
        self.events.callbacks["server-login"](
            SimpleNamespace(success=self.login_success, reason=self.reason)
        )

    def disconnect(self):
        self.disconnected = True

    def quit(self):
        self.quit_called = True


class Status:
    FINISHED = "Finished"
    CANCELLED = "Cancelled"
    CONNECTION_TIMEOUT = "Connection timeout"
    CONNECTION_CLOSED = "Connection closed"
    LOCAL_FILE_ERROR = "Local file error"
    USER_LOGGED_OFF = "User logged off"
    DOWNLOAD_FOLDER_ERROR = "Download folder error"
    FILTERED = "Filtered"


def patch_nicotine(monkeypatch, *, login_success=True, reason=""):
    import soulseek.client as client_module

    events = DummyEvents()
    config = DummyConfig()
    core = DummyCore(events, login_success=login_success, reason=reason)

    def fake_import(name):
        return {
            "pynicotine.core": SimpleNamespace(core=core),
            "pynicotine.config": SimpleNamespace(config=config),
            "pynicotine.events": SimpleNamespace(events=events),
            "pynicotine.transfers": SimpleNamespace(TransferStatus=Status),
            "pynicotine.slskmessages": SimpleNamespace(FileListMessage=DummySearch),
        }[name]

    monkeypatch.setattr(client_module.importlib, "import_module", fake_import)
    return core, config, events


async def initialized_cache(tmp_path: Path) -> DownloadCache:
    cache = DownloadCache(tmp_path / "soulseek")
    await cache.initialize()
    return cache


def test_successful_login_persists_stable_credentials_and_disables_sharing(
    tmp_path, monkeypatch
):
    core, config, _events = patch_nicotine(monkeypatch)

    async def run():
        cache = await initialized_cache(tmp_path)
        client = SoulseekClient(
            loop=asyncio.get_running_loop(),
            cache=cache,
            listen_port=3456,
            logger=SimpleNamespace(warning=lambda *args: None),
        )
        await client.start()
        first = json.loads(cache.credentials_path.read_text(encoding="utf-8"))
        assert config.sections["transfers"]["shared"] == []
        assert config.sections["transfers"]["buddyshared"] == []
        assert config.sections["transfers"]["trustedshared"] == []
        assert config.sections["transfers"]["remotedownloads"] is False
        assert config.sections["transfers"]["uploadallowed"] == 0
        assert config.sections["transfers"]["uploadslots"] == 0
        assert "uploads" not in core.components
        assert config.sections["server"]["portrange"] == (3456, 3456)
        await client.stop()
        assert core.disconnected and core.quit_called

        _core2, config2, _events2 = patch_nicotine(monkeypatch)
        second_client = SoulseekClient(
            loop=asyncio.get_running_loop(),
            cache=cache,
            listen_port=3456,
            logger=SimpleNamespace(warning=lambda *args: None),
        )
        await second_client.start()
        assert config2.sections["server"]["login"] == first["username"]
        assert config2.sections["server"]["passw"] == first["password"]
        await second_client.stop()

    asyncio.run(run())
    assert (tmp_path / "soulseek" / "credentials.json").stat().st_mode & 0o077 == 0


def test_failed_login_including_port_conflict_does_not_persist_credentials(
    tmp_path, monkeypatch
):
    patch_nicotine(
        monkeypatch, login_success=False, reason="listen port already in use"
    )

    async def run():
        cache = await initialized_cache(tmp_path)
        client = SoulseekClient(
            loop=asyncio.get_running_loop(),
            cache=cache,
            listen_port=2234,
            logger=SimpleNamespace(warning=lambda *args: None),
        )
        with pytest.raises(SetupFailedError, match="listen port already in use"):
            await client.start()
        assert not cache.credentials_path.exists()

    asyncio.run(run())


def test_search_responses_are_isolated_by_token_and_malformed_rows_are_ignored(
    tmp_path,
):
    async def run():
        cache = await initialized_cache(tmp_path)
        client = SoulseekClient(
            loop=asyncio.get_running_loop(),
            cache=cache,
            listen_port=2234,
            logger=SimpleNamespace(warning=lambda *args: None),
        )
        client._core = SimpleNamespace(search=DummySearch())
        client._file_list_message = DummySearch
        client._search_results = {11: [], 22: []}
        client._on_search_response(
            SimpleNamespace(
                token=11,
                search_username="peer-a",
                    freeulslots=True,
                    inqueue=2,
                    ulspeed=4096,
                list=[(1, r"Music\A.mp3", 100, "mp3", [1]), ("broken",)],
            )
        )
        client._on_search_response(
            SimpleNamespace(
                token=22,
                search_username="peer-b",
                list=[(1, r"Music\B.flac", 200, "flac", [2])],
            )
        )
        assert [item.username for item in client._search_results[11]] == ["peer-a"]
        peer_a = client._search_results[11][0]
        assert peer_a.free_upload_slots is True
        assert peer_a.queue_size == 2
        assert peer_a.upload_speed == 4096
        assert [item.username for item in client._search_results[22]] == ["peer-b"]

        client._file_list_message = SimpleNamespace(
            parse_audio_quality_length=lambda size, attrs: (
                "unknown",
                "bad",
                "unknown",
                "bad",
            )
        )
        client._on_search_response(
            SimpleNamespace(
                token=11,
                search_username="peer-c",
                list=[(1, r"Music\C.mp3", "bad-size", "mp3", [3])],
            )
        )
        malformed = client._search_results[11][-1]
        assert malformed.duration is None
        assert malformed.bitrate == 0
        assert malformed.size == 0

    asyncio.run(run())


def test_abort_clears_transfer_and_removes_only_incomplete_file(tmp_path):
    async def run():
        cache = await initialized_cache(tmp_path)
        incomplete = cache.incomplete_path / "candidate.mp3"
        incomplete.write_bytes(b"partial")
        client = SoulseekClient(
            loop=asyncio.get_running_loop(),
            cache=cache,
            listen_port=2234,
            logger=SimpleNamespace(warning=lambda *args: None),
        )
        client._core = SimpleNamespace(downloads=DummyDownloads())
        client._transfer_status = Status
        transfer = SimpleNamespace()
        handle = DownloadHandle(
            peer_file=PeerFile("peer", "candidate.mp3", extension="mp3"),
            folder=cache.downloads_path,
            context={},
            transfer=transfer,
            path=str(incomplete),
        )
        client._handles[client._handle_key("peer", "candidate.mp3")] = handle
        await client.abort_download(handle)
        assert not incomplete.exists()
        assert client._core.downloads.aborted
        assert client._core.downloads.cleared

    asyncio.run(run())
