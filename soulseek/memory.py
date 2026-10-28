"""Memory-only receive sink and adapter for the pinned Nicotine+ core."""

from __future__ import annotations

import threading
import time
from typing import Any

MAX_AUDIO_BYTES = 256 * 1024 * 1024


class AudioBuffer:
    """Single network writer, independent playback cursors, no filesystem I/O.

    Keep bytes until the last consumer releases the transfer. A hard limit makes
    a malicious peer or a very large file fail without exhausting host memory.
    """

    def __init__(self, notify: Any, limit: int = MAX_AUDIO_BYTES) -> None:
        self._data = bytearray()
        self._lock = threading.Lock()
        self._notify = notify
        self._limit = limit
        self._closed = False

    def write(self, data: bytes) -> int:
        with self._lock:
            if self._closed:
                raise OSError("Soulseek audio buffer is closed")
            if len(self._data) + len(data) > self._limit:
                raise OSError("Soulseek audio exceeds the 256 MiB memory limit")
            self._data.extend(data)
        self._notify()
        return len(data)

    def read(self, offset: int, size: int) -> bytes:
        with self._lock:
            return bytes(self._data[offset:offset + size])

    def __len__(self) -> int:
        with self._lock:
            return len(self._data)

    def close(self) -> None:
        # Nicotine closes its sink on completion; consumers still need the bytes.
        with self._lock:
            self._closed = True
        self._notify()

    def release(self) -> None:
        with self._lock:
            self._closed = True
            self._data.clear()


def create_memory_downloads(client: Any) -> Any:
    """Override disk operations at the transfer boundary, before core.start().

    These hooks match nicotine-plus==3.3.11. The networking thread accepts a
    file-like sink in DownloadFile and calls only write() on received audio.
    """
    from pynicotine.core import core
    from pynicotine.downloads import Downloads
    from pynicotine.slskmessages import CloseConnection, DownloadFile, FileOffset
    from pynicotine.transfers import Transfers, TransferStatus

    class MemoryDownloads(Downloads):
        def _load_transfers(self) -> None:
            # Never resume old disk downloads after upgrading the provider.
            pass

        def _save_transfers(self) -> None:
            pass

        def _delete_stale_incomplete_downloads(self) -> None:
            pass

        def get_complete_download_file_path(self, *args: Any, **kwargs: Any) -> tuple[str, bool]:
            return "", False

        def _file_transfer_init(self, msg: Any) -> None:
            if msg.is_outgoing:
                return
            download = self.active_users.get(msg.username, {}).get(msg.token)
            key = client._handle_key(msg.username, download.virtual_path) if download else ""
            with client._handles_lock:
                handle = client._handles.get(key)
            if download is None or download.sock is not None or handle is None:
                core.send_message_to_network_thread(CloseConnection(msg.sock))
                return
            # Always start at byte zero. Resuming a failed peer after audio has
            # been emitted would conceal a broken stream from the consumer.
            download.sock = msg.sock
            download.file_handle = handle.buffer
            download.last_byte_offset = 0
            download.start_time = time.monotonic() - download.time_elapsed
            download.status = TransferStatus.TRANSFERRING
            core.send_message_to_network_thread(DownloadFile(
                sock=msg.sock, token=msg.token, file=handle.buffer, leftbytes=download.size
            ))
            core.send_message_to_peer(msg.username, FileOffset(msg.sock, 0))
            self._update_transfer(download)

        def _finish_transfer(self, transfer: Any) -> None:
            # Base bookkeeping closes the sink; Downloads would move it to disk.
            Transfers._finish_transfer(self, transfer)
            self._update_transfer(transfer)

    return MemoryDownloads()
