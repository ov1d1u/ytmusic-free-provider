"""Lifecycle-managed bridge to the headless Nicotine+ networking core."""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import secrets
import string
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from music_assistant_models.errors import SetupFailedError

from .cache import DownloadCache
from .ranking import PeerFile, deduplicate_variants

SEARCH_WINDOW_SECONDS = 8
MAX_SEARCH_RESULTS = 100


def _nonnegative_int(value: Any) -> int | None:
    """Parse a peer metric without letting malformed responses sink a search."""

    try:
        return max(int(value), 0)
    except (TypeError, ValueError, OverflowError):
        return None


@dataclass(slots=True)
class DownloadHandle:
    peer_file: PeerFile
    folder: Path
    context: dict[str, Any]
    transfer: Any = None
    path: str = ""
    status: str = "Queued"
    finished: bool = False
    failed: bool = False
    error: str = ""
    updated: asyncio.Event = field(default_factory=asyncio.Event)


class SoulseekClient:
    """Own Nicotine+'s process-global core and bridge its events to asyncio."""

    def __init__(
        self,
        *,
        loop: asyncio.AbstractEventLoop,
        cache: DownloadCache,
        listen_port: int,
        logger: Any,
        on_download_complete: Callable[[DownloadHandle], None] | None = None,
    ) -> None:
        self.loop = loop
        self.cache = cache
        self.listen_port = listen_port
        self.logger = logger
        self.on_download_complete = on_download_complete
        self._core: Any = None
        self._config: Any = None
        self._events: Any = None
        self._transfer_status: Any = None
        self._file_list_message: Any = None
        self._lock = threading.RLock()
        self._event_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._login_event = asyncio.Event()
        self._login_error = ""
        self._search_results: dict[int, list[PeerFile]] = {}
        self._search_lock = threading.Lock()
        self._handles: dict[str, DownloadHandle] = {}
        self._handles_lock = threading.Lock()
        self._started = False

    async def start(self) -> None:
        if self._started:
            return
        self._login_event.clear()
        self._login_error = ""
        await asyncio.to_thread(self._start_sync)
        try:
            await asyncio.wait_for(self._login_event.wait(), timeout=30)
        except TimeoutError as err:
            await self.stop()
            raise SetupFailedError(
                "Soulseek login timed out. Check outbound connectivity and the listen port."
            ) from err
        if self._login_error:
            await self.stop()
            raise SetupFailedError(f"Soulseek login failed: {self._login_error}")
        self._persist_credentials(self._pending_credentials)
        self._started = True

    def _start_sync(self) -> None:
        try:
            self._core = importlib.import_module("pynicotine.core").core
            self._config = importlib.import_module("pynicotine.config").config
            self._events = importlib.import_module("pynicotine.events").events
            self._transfer_status = importlib.import_module(
                "pynicotine.transfers"
            ).TransferStatus
            self._file_list_message = importlib.import_module(
                "pynicotine.slskmessages"
            ).FileListMessage
        except (ImportError, AttributeError) as err:
            raise SetupFailedError(
                "Nicotine+ 3.3.11 is unavailable; reload the provider dependencies"
            ) from err

        credentials = self._load_credentials() or self._generate_credentials()
        self._pending_credentials = credentials
        data_path = self.cache.nicotine_data_path
        data_path.mkdir(parents=True, exist_ok=True)
        self._config.set_data_folder(str(data_path))
        self._config.set_config_file(str(data_path / "config"))
        components = {
            "error_handler",
            "network_thread",
            "shares",
            "users",
            "notifications",
            "network_filter",
            "statistics",
            "search",
            "downloads",
            "buddies",
            "pluginhandler",
        }
        with self._lock:
            self._core.init_components(components, isolated_mode=True)
            self._configure(credentials)
            self._events.connect("server-login", self._on_server_login)
            self._events.connect("server-disconnect", self._on_server_disconnect)
            self._events.connect("file-search-response", self._on_search_response)
            self._events.connect("update-download", self._on_download_update)
            self._core.start()
            self._core.connect()
        self._stop_event.clear()
        self._event_thread = threading.Thread(
            target=self._event_loop,
            name="music-assistant-soulseek-events",
            daemon=True,
        )
        self._event_thread.start()

    def _configure(self, credentials: tuple[str, str]) -> None:
        username, password = credentials
        sections = self._config.sections
        sections["server"]["login"] = username
        sections["server"]["passw"] = password
        sections["server"]["portrange"] = (self.listen_port, self.listen_port)
        sections["server"]["auto_connect_startup"] = False
        sections["server"]["upnp"] = False
        transfers = sections["transfers"]
        transfers["downloaddir"] = str(self.cache.downloads_path)
        transfers["incompletedir"] = str(self.cache.incomplete_path)
        transfers["shared"] = []
        transfers["buddyshared"] = []
        transfers["trustedshared"] = []
        transfers["remotedownloads"] = False
        transfers["uploadallowed"] = 0
        transfers["uploadslots"] = 0
        if "searches" in sections:
            sections["searches"]["maxresults"] = MAX_SEARCH_RESULTS
        self._config.write_configuration()

    def _event_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                with self._lock:
                    keep_processing = self._events.process_thread_events()
                if keep_processing is False:
                    break
            except Exception as err:  # noqa: BLE001  # pragma: no cover - external core
                self.logger.warning("Soulseek event processing failed: %s", err)
            self._stop_event.wait(0.05)

    def _load_credentials(self) -> tuple[str, str] | None:
        try:
            value = json.loads(self.cache.credentials_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return None
        username = str(value.get("username") or "")
        password = str(value.get("password") or "")
        return (username, password) if username and password else None

    @staticmethod
    def _generate_credentials() -> tuple[str, str]:
        alphabet = string.ascii_lowercase + string.digits
        username = "ma_" + "".join(secrets.choice(alphabet) for _ in range(16))
        password = secrets.token_urlsafe(24)
        return username, password

    def _persist_credentials(self, credentials: tuple[str, str]) -> None:
        temporary = self.cache.credentials_path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(
                {"username": credentials[0], "password": credentials[1]}, indent=2
            ),
            encoding="utf-8",
        )
        os.chmod(temporary, 0o600)
        os.replace(temporary, self.cache.credentials_path)

    def _on_server_login(self, msg: Any) -> None:
        success = bool(getattr(msg, "success", False))
        reason = str(getattr(msg, "reason", "unknown server response"))

        def apply() -> None:
            self._login_error = "" if success else reason
            self._login_event.set()

        self.loop.call_soon_threadsafe(apply)

    def _on_server_disconnect(self, msg: Any) -> None:
        if not self._stop_event.is_set():
            self.logger.warning(
                "Soulseek disconnected: %s", getattr(msg, "reason", "unknown reason")
            )

    def _on_search_response(self, msg: Any) -> None:
        try:
            token = int(getattr(msg, "token", -1))
        except (TypeError, ValueError, OverflowError):
            return
        with self._search_lock:
            results = self._search_results.get(token)
            if results is None or len(results) >= MAX_SEARCH_RESULTS:
                return
            username = str(
                getattr(msg, "username", "") or getattr(msg, "search_username", "")
            )
            free_upload_slots = getattr(msg, "freeulslots", None)
            if free_upload_slots is not None:
                free_upload_slots = bool(free_upload_slots)
            queue_size = _nonnegative_int(getattr(msg, "inqueue", None))
            upload_speed = _nonnegative_int(getattr(msg, "ulspeed", None))
            for raw in getattr(msg, "list", ()) or ():
                if len(results) >= MAX_SEARCH_RESULTS:
                    break
                try:
                    _code, path, size, extension, attributes = raw[:5]
                except (TypeError, ValueError, IndexError):
                    continue
                try:
                    _human_quality, bitrate, _human_length, duration = (
                        self._file_list_message.parse_audio_quality_length(
                            size, attributes
                        )
                    )
                except (TypeError, ValueError, IndexError):
                    bitrate = 0
                    duration = None
                try:
                    parsed_duration = float(duration) if duration else None
                    if parsed_duration is not None and parsed_duration <= 0:
                        parsed_duration = None
                except (TypeError, ValueError, OverflowError):
                    parsed_duration = None
                try:
                    parsed_bitrate = int(bitrate or 0)
                except (TypeError, ValueError, OverflowError):
                    parsed_bitrate = 0
                try:
                    parsed_size = int(size or 0)
                except (TypeError, ValueError, OverflowError):
                    parsed_size = 0
                candidate = PeerFile(
                    username=username,
                    path=str(path),
                    size=parsed_size,
                    extension=str(extension or ""),
                    bitrate=parsed_bitrate,
                    duration=parsed_duration,
                    file_attributes=(
                        dict(attributes)
                        if isinstance(attributes, dict)
                        else tuple(attributes or ())
                    ),
                    free_upload_slots=free_upload_slots,
                    queue_size=queue_size,
                    upload_speed=upload_speed,
                )
                candidate_key = (
                    candidate.username.casefold(),
                    candidate.path,
                    candidate.size,
                )
                existing_keys = {
                    (item.username.casefold(), item.path, item.size) for item in results
                }
                if (
                    candidate.normalized_extension in {"flac", "mp3"}
                    and candidate_key not in existing_keys
                ):
                    results.append(candidate)

    @staticmethod
    def _handle_key(username: str, path: str) -> str:
        return f"{username}\0{path}"

    def _on_download_update(self, transfer: Any, update_parent: bool = True) -> None:
        del update_parent
        key = self._handle_key(str(transfer.username), str(transfer.virtual_path))
        with self._handles_lock:
            handle = self._handles.get(key)
        if handle is None:
            return
        try:
            path = str(
                self._core.downloads.get_current_download_file_path(transfer) or ""
            )
        except Exception as err:  # noqa: BLE001 - external transfer may be half-removed
            self.logger.debug(
                "Could not resolve current Soulseek download path: %s", err
            )
            path = handle.path
        status = str(getattr(transfer, "status", ""))
        finished_value = str(getattr(self._transfer_status, "FINISHED", "Finished"))
        failed_values = {
            str(getattr(self._transfer_status, name, name))
            for name in (
                "CANCELLED",
                "CONNECTION_TIMEOUT",
                "CONNECTION_CLOSED",
                "FILE_READ_ERROR",
                "DOWNLOAD_FOLDER_ERROR",
                "FILTERED",
                "LOCAL_FILE_ERROR",
                "REMOTE_FILE_ERROR",
                "USER_LOGGED_OFF",
            )
        }
        failed_values.update(
            {
                "File read error.",
                "File not shared.",
                "Complete",
                "Banned",
                "Pending shutdown.",
                "Too many files",
                "Too many megabytes",
                "Disallowed extension",
            }
        )

        def apply() -> None:
            handle.path = path or handle.path
            handle.status = status
            handle.finished = status == finished_value
            handle.failed = status in failed_values
            handle.error = status if handle.failed else ""
            handle.updated.set()
            if handle.finished and self.on_download_complete:
                self.on_download_complete(handle)

        self.loop.call_soon_threadsafe(apply)

    async def search_variants(self, query: str) -> list[PeerFile]:
        def begin() -> int:
            with self._lock:
                self._core.search.do_search(query, "global")
                token = int(self._core.search.token)
                # Register before the event pump can acquire _lock and process a
                # very fast response for this token.
                with self._search_lock:
                    self._search_results[token] = []
            return token

        token = await asyncio.to_thread(begin)
        await asyncio.sleep(SEARCH_WINDOW_SECONDS)
        with self._search_lock:
            results = self._search_results.pop(token, [])

        def finish() -> None:
            try:
                with self._lock:
                    self._core.search.remove_search(token)
            except (AttributeError, KeyError):
                pass

        await asyncio.to_thread(finish)
        return deduplicate_variants(results)[:MAX_SEARCH_RESULTS]

    async def start_download(
        self,
        peer_file: PeerFile,
        *,
        folder: Path,
        context: dict[str, Any],
    ) -> DownloadHandle:
        key = self._handle_key(peer_file.username, peer_file.path)
        with self._handles_lock:
            existing = self._handles.get(key)
        if existing is not None and not existing.failed:
            return existing
        handle = DownloadHandle(peer_file=peer_file, folder=folder, context=context)
        with self._handles_lock:
            self._handles[key] = handle

        def enqueue() -> tuple[Any, str]:
            with self._lock:
                self._core.downloads.enqueue_download(
                    peer_file.username,
                    peer_file.path,
                    folder_path=str(folder),
                    size=peer_file.size,
                    file_attributes=peer_file.file_attributes,
                )
                transfer = self._core.downloads.transfers.get(
                    peer_file.username + peer_file.path
                )
                if transfer is None:
                    raise RuntimeError("Nicotine+ did not create a download transfer")
                path = self._core.downloads.get_current_download_file_path(transfer)
                return transfer, str(path or "")

        try:
            handle.transfer, handle.path = await asyncio.to_thread(enqueue)
        except Exception:
            # Preserve the external peer error for provider-level failover.
            with self._handles_lock:
                self._handles.pop(key, None)
            raise
        self._on_download_update(handle.transfer)
        return handle

    async def abort_download(self, handle: DownloadHandle) -> None:
        transfer = handle.transfer

        def abort() -> None:
            if transfer is None:
                return
            with self._lock:
                try:
                    self._core.downloads.abort_downloads(
                        [transfer], status=self._transfer_status.CANCELLED
                    )
                finally:
                    try:
                        self._core.downloads.clear_downloads([transfer])
                    except Exception as err:  # noqa: BLE001 - best-effort external cleanup
                        self.logger.debug(
                            "Could not clear aborted Soulseek transfer: %s", err
                        )

        await asyncio.to_thread(abort)
        with self._handles_lock:
            self._handles.pop(
                self._handle_key(handle.peer_file.username, handle.peer_file.path), None
            )
        await self.cache.remove_incomplete(handle.path)

    async def stop(self) -> None:
        if self._core is None:
            return

        def shutdown() -> None:
            with self._lock:
                try:
                    self._core.disconnect()
                finally:
                    try:
                        self._core.quit()
                    except Exception as err:  # noqa: BLE001 - best-effort external shutdown
                        self.logger.debug("Nicotine+ quit reported an error: %s", err)

        await asyncio.to_thread(shutdown)
        self._stop_event.set()
        if self._event_thread and self._event_thread.is_alive():
            await asyncio.to_thread(self._event_thread.join, 5)
        if self._event_thread and self._event_thread.is_alive():
            await asyncio.to_thread(self._event_thread.join, 1)
        self._started = False
