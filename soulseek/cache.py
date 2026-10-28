"""Bounded persistent download cache for the Soulseek provider."""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

PLAYABLE_EXTENSIONS = frozenset({"flac", "mp3"})
DEFAULT_LIMIT_BYTES = 5 * 1024**3


@dataclass(slots=True)
class CacheEntry:
    """One completed, locally replayable Soulseek download."""

    track_id: str
    path: str
    extension: str
    size: int
    duration: int | None = None
    bitrate: int = 0
    last_accessed: float = 0.0


class DownloadCache:
    """Index completed files, evicting least-recently-used entries by size."""

    def __init__(self, root: Path, limit_bytes: int = DEFAULT_LIMIT_BYTES) -> None:
        self.root = root
        self.downloads_path = root / "downloads"
        self.incomplete_path = root / "incomplete"
        self.index_path = root / "cache_index.json"
        self.credentials_path = root / "credentials.json"
        self.nicotine_data_path = root / "nicotine"
        self.limit_bytes = max(1, int(limit_bytes))
        self._entries: dict[str, CacheEntry] = {}
        self._active_tracks: dict[str, int] = {}
        self._lock = asyncio.Lock()

    async def initialize(self) -> None:
        async with self._lock:
            await asyncio.to_thread(self._initialize_sync)

    def _initialize_sync(self) -> None:
        for path in (self.root, self.downloads_path, self.incomplete_path, self.nicotine_data_path):
            path.mkdir(parents=True, exist_ok=True)
        try:
            raw = json.loads(self.index_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            raw = {}

        changed = False
        for value in raw.values() if isinstance(raw, dict) else ():
            if not isinstance(value, dict):
                changed = True
                continue
            candidate = dict(value)
            candidate.pop("profile", None)  # pre-LRU index compatibility
            try:
                path = self._contained_path(
                    Path(str(candidate["path"])), self.downloads_path
                )
                if path is None:
                    raise ValueError
                extension = str(candidate.get("extension") or path.suffix).casefold().removeprefix(".")
                if extension not in PLAYABLE_EXTENSIONS:
                    raise ValueError
                stat = path.stat()
                if not path.is_file() or stat.st_size <= 0:
                    raise FileNotFoundError
                entry = CacheEntry(
                    track_id=str(candidate["track_id"]),
                    path=str(path),
                    extension=extension,
                    size=stat.st_size,
                    duration=candidate.get("duration"),
                    bitrate=int(candidate.get("bitrate") or 0),
                    last_accessed=float(candidate.get("last_accessed") or stat.st_mtime),
                )
            except (KeyError, TypeError, ValueError, OSError):
                changed = True
                continue
            self._entries[self._key(entry.track_id, entry.extension)] = entry

        # Context files make completed transfers recoverable after a crash
        # between Nicotine's completion event and the atomic index update.
        for context_path in self.downloads_path.rglob(".cache-context.json"):
            try:
                context = json.loads(context_path.read_text(encoding="utf-8"))
                track_id = str(context["track_id"])
            except (OSError, json.JSONDecodeError, KeyError, TypeError):
                continue
            candidates = sorted(
                (
                    path for path in context_path.parent.rglob("*")
                    if path.is_file()
                    and path.name != context_path.name
                    and path.suffix.casefold().removeprefix(".") in PLAYABLE_EXTENSIONS
                    and path.stat().st_size > 0
                ),
                key=lambda path: (-path.stat().st_size, str(path)),
            )
            if not candidates:
                continue
            path = candidates[0]
            extension = path.suffix.casefold().removeprefix(".")
            key = self._key(track_id, extension)
            if key not in self._entries:
                stat = path.stat()
                self._entries[key] = CacheEntry(
                    track_id=track_id,
                    path=str(path),
                    extension=extension,
                    size=stat.st_size,
                    duration=context.get("duration"),
                    bitrate=int(context.get("bitrate") or 0),
                    last_accessed=stat.st_mtime,
                )
                changed = True

        changed = self._evict_sync() or changed
        if changed:
            self._write_sync()

    @staticmethod
    def _key(track_id: str, extension: str) -> str:
        return f"{extension}:{track_id}"

    @staticmethod
    def _contained_path(path: Path, parent: Path) -> Path | None:
        """Resolve a persisted path and reject anything outside provider storage."""

        try:
            resolved = path.resolve(strict=True)
            resolved.relative_to(parent.resolve())
        except (OSError, ValueError):
            return None
        return resolved

    async def lookup(self, track_id: str, preferred_extension: str) -> CacheEntry | None:
        """Return preferred cached format then fallback, updating LRU state."""

        other = "mp3" if preferred_extension == "flac" else "flac"
        order = (preferred_extension, other) if preferred_extension in PLAYABLE_EXTENSIONS else ("mp3", "flac")
        async with self._lock:
            changed = False
            for extension in order:
                key = self._key(track_id, extension)
                entry = self._entries.get(key)
                if entry is None:
                    continue
                path = Path(entry.path)
                try:
                    stat = path.stat()
                    valid = path.is_file() and stat.st_size > 0
                except OSError:
                    valid = False
                if not valid:
                    self._entries.pop(key, None)
                    changed = True
                    continue
                entry.size = stat.st_size
                entry.last_accessed = time.time()
                await asyncio.to_thread(self._write_sync)
                return entry
            if changed:
                await asyncio.to_thread(self._write_sync)
        return None

    def download_folder(self, track_id: str, profile: str) -> Path:
        safe_id = re.sub(r"[^A-Za-z0-9._-]+", "_", track_id).strip("._") or "track"
        folder = self.downloads_path / safe_id / profile
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    async def prepare_download(self, *, track_id: str, profile: str, duration: int | None, bitrate: int) -> Path:
        folder = self.download_folder(track_id, profile)
        context_path = folder / ".cache-context.json"
        payload = {"track_id": track_id, "profile": profile, "duration": duration, "bitrate": bitrate}

        def write() -> None:
            temporary = context_path.with_suffix(".tmp")
            temporary.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
            os.replace(temporary, context_path)

        async with self._lock:
            await asyncio.to_thread(write)
        return folder

    async def set_active(self, track_id: str, active: bool) -> None:
        """Protect or release all completed files for an actively played track."""

        async with self._lock:
            if active:
                self._active_tracks[track_id] = self._active_tracks.get(track_id, 0) + 1
                return
            remaining = self._active_tracks.get(track_id, 0) - 1
            if remaining > 0:
                self._active_tracks[track_id] = remaining
            else:
                self._active_tracks.pop(track_id, None)
            if self._evict_sync():
                await asyncio.to_thread(self._write_sync)

    async def record(self, *, track_id: str, profile: str, path: str | Path, duration: int | None, bitrate: int) -> CacheEntry | None:
        del profile  # retained in the caller/context contract for compatibility
        resolved = self._contained_path(Path(path), self.downloads_path)
        if resolved is None:
            return None
        try:
            stat = resolved.stat()
        except OSError:
            return None
        extension = resolved.suffix.casefold().removeprefix(".")
        if not resolved.is_file() or stat.st_size <= 0 or extension not in PLAYABLE_EXTENSIONS:
            return None
        entry = CacheEntry(
            track_id=track_id,
            path=str(resolved),
            extension=extension,
            size=stat.st_size,
            duration=duration,
            bitrate=bitrate,
            last_accessed=time.time(),
        )
        async with self._lock:
            key = self._key(track_id, extension)
            previous = self._entries.get(key)
            self._entries[key] = entry
            if previous and previous.path != entry.path:
                self._unlink_entry_sync(previous)
            self._evict_sync()
            await asyncio.to_thread(self._write_sync)
        return entry

    def _evict_sync(self) -> bool:
        changed = False
        total = sum(max(entry.size, 0) for entry in self._entries.values())
        if total <= self.limit_bytes:
            return changed
        for key, entry in sorted(self._entries.items(), key=lambda item: (item[1].last_accessed, item[0])):
            if total <= self.limit_bytes:
                break
            if entry.track_id in self._active_tracks:
                continue
            self._entries.pop(key, None)
            total -= max(entry.size, 0)
            self._unlink_entry_sync(entry)
            changed = True
        return changed

    def _unlink_entry_sync(self, entry: CacheEntry) -> None:
        path = self._contained_path(Path(entry.path), self.downloads_path)
        if path is None:
            return
        path.unlink(missing_ok=True)
        parent = path.parent
        while parent != self.downloads_path:
            try:
                for context in parent.glob(".cache-context.*"):
                    context.unlink(missing_ok=True)
                parent.rmdir()
            except OSError:
                break
            parent = parent.parent

    def _write_sync(self) -> None:
        payload = {key: asdict(value) for key, value in sorted(self._entries.items())}
        temporary = self.index_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(temporary, self.index_path)

    async def remove_incomplete(self, path: str | Path | None) -> None:
        if not path:
            return
        candidate = self._contained_path(Path(path), self.incomplete_path)
        if candidate is None:
            return
        try:
            await asyncio.to_thread(candidate.unlink)
        except FileNotFoundError:
            pass

    def debug_snapshot(self) -> dict[str, dict[str, Any]]:
        return {key: asdict(value) for key, value in self._entries.items()}
