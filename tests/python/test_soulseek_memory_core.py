"""Offline contract with the real pinned Nicotine+ transfer and network code."""

import asyncio
import builtins
import importlib
from types import SimpleNamespace

import pytest

from soulseek.client import SoulseekClient
from soulseek.memory import create_memory_downloads
from soulseek.ranking import PeerFile
from soulseek.storage import RuntimeStorage


def test_real_core_streams_into_memory_without_opening_files(tmp_path, monkeypatch):
    pytest.importorskip("pynicotine")
    core_module = importlib.import_module("pynicotine.core")
    downloads_module = importlib.import_module("pynicotine.downloads")
    transfers_module = importlib.import_module("pynicotine.transfers")
    from pynicotine.slskmessages import DownloadFile
    from pynicotine.slskproto import NetworkThread
    from pynicotine.transfers import Transfer, TransferStatus

    async def run():
        messages = []
        fake_core = SimpleNamespace(
            send_message_to_network_thread=messages.append,
            send_message_to_peer=lambda *args: None,
            statistics=SimpleNamespace(append_stat_value=lambda *args: None),
            users=SimpleNamespace(unwatch_user=lambda *args, **kwargs: None),
        )
        client = SoulseekClient(loop=asyncio.get_running_loop(),
                                storage=RuntimeStorage(tmp_path), listen_port=2234,
                                logger=SimpleNamespace(debug=lambda *args: None))
        fake_events = SimpleNamespace(connect=lambda *args: None,
                                      emit=lambda name, transfer, *args: (
                                          client._on_download_update(transfer)
                                          if name in {"update-download", "abort-download"} else None))
        monkeypatch.setattr(core_module, "core", fake_core)
        monkeypatch.setattr(downloads_module, "core", fake_core)
        monkeypatch.setattr(transfers_module, "core", fake_core)
        monkeypatch.setattr(downloads_module, "events", fake_events)
        monkeypatch.setattr(transfers_module, "events", fake_events)
        downloads = create_memory_downloads(client)
        client._core = SimpleNamespace(downloads=downloads)
        client._transfer_status = TransferStatus

        # Enqueue only is mocked: all receive/progress/completion operations use
        # the pinned library. No network or Soulseek server is contacted.
        transfer = Transfer("peer", "song.mp3", "memory", 10, {})
        transfer.token = 42
        downloads.transfers["peersong.mp3"] = transfer
        downloads.active_users["peer"][42] = transfer
        monkeypatch.setattr(type(downloads), "enqueue_download", lambda *args, **kwargs: None)
        handle = await client.start_download(PeerFile("peer", "song.mp3", 10, "mp3"))

        def forbid_disk(*args, **kwargs):
            raise AssertionError("audio path attempted filesystem I/O")
        monkeypatch.setattr(builtins, "open", forbid_disk)
        downloads._load_transfers()
        downloads._save_transfers()
        downloads._file_transfer_init(SimpleNamespace(
            is_outgoing=False, username="peer", token=42, sock="socket"))
        receive = next(msg for msg in messages if isinstance(msg, DownloadFile))
        assert receive.file is handle.buffer
        network = SimpleNamespace(_total_download_bandwidth=0)
        NetworkThread._write_download_file(network, receive, b"first", 5)
        await asyncio.sleep(0)
        assert handle.buffer.read(0, 100) == b"first"
        assert not handle.finished
        NetworkThread._write_download_file(network, receive, b"last!", 5)
        downloads._file_download_progress("peer", 42, "socket", 0)
        downloads._file_connection_closed("peer", 42, "socket")
        await asyncio.sleep(0)
        assert handle.finished
        assert handle.buffer.read(0, 100) == b"firstlast!"
        await client.abort_download(handle)
        assert not downloads.transfers
        assert len(handle.buffer) == 0
        assert list(tmp_path.iterdir()) == []

    asyncio.run(run())
