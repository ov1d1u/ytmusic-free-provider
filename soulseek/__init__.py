"""Experimental MusicBrainz/Soulseek provider for Music Assistant."""

from __future__ import annotations

__version__ = "0.2.0"

import asyncio
import re
import time
from collections.abc import AsyncGenerator
from contextlib import suppress
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING, Any

from music_assistant.helpers.util import infer_album_type
from music_assistant.models.music_provider import MusicProvider
from music_assistant_models.config_entries import (
    ConfigEntry,
    ConfigValueOption,
    ConfigValueType,
)
from music_assistant_models.enums import (
    AlbumType,
    ConfigEntryType,
    ContentType,
    ImageType,
    MediaType,
    ProviderFeature,
    StreamType,
)
from music_assistant_models.errors import InvalidDataError, UnplayableMediaError
from music_assistant_models.media_items import (
    Album,
    Artist,
    AudioFormat,
    ItemMapping,
    MediaItemImage,
    ProviderMapping,
    SearchResults,
    Track,
    UniqueList,
)
from music_assistant_models.streamdetails import StreamDetails

from .cache import DownloadCache
from .client import DownloadHandle, SoulseekClient
from .musicbrainz import MusicBrainzClient, MusicBrainzItem
from .ranking import diversify_peers, rank_variants

if TYPE_CHECKING:
    from music_assistant import MusicAssistant
    from music_assistant.models import ProviderInstanceType
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest


CONF_AUDIO_QUALITY = "audio_quality"
CONF_LISTEN_PORT = "listen_port"
CONF_CACHE_LIMIT_GB = "cache_limit_gb"
QUALITY_STANDARD = "mp3"
QUALITY_HIGH = "flac"
DEFAULT_LISTEN_PORT = 2234
DEFAULT_CACHE_LIMIT_GB = 5
MIN_CACHE_LIMIT_GB = 1
MAX_CACHE_LIMIT_GB = 1000
FIRST_BYTE_TIMEOUT = 20
MIDSTREAM_STALL_TIMEOUT = 20
STREAM_CHUNK_SIZE = 64 * 1024

SUPPORTED_FEATURES = {
    ProviderFeature.SEARCH,
    ProviderFeature.ARTIST_ALBUMS,
    ProviderFeature.ARTIST_TOPTRACKS,
}


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Create one Soulseek provider instance."""

    return SoulseekProvider(mass, manifest, config, SUPPORTED_FEATURES)


async def get_config_entries(
    mass: MusicAssistant,
    instance_id: str | None = None,
    action: str | None = None,
    values: dict[str, ConfigValueType] | None = None,
) -> tuple[ConfigEntry, ...]:
    """Return setup entries for Music Assistant 2.9."""

    return _build_config_entries()


def _build_config_entries() -> tuple[ConfigEntry, ...]:
    return (
        ConfigEntry(
            key=CONF_AUDIO_QUALITY,
            type=ConfigEntryType.STRING,
            label="Audio quality",
            default_value=QUALITY_STANDARD,
            required=True,
            options=(
                ConfigValueOption(title="Standard (MP3)", value=QUALITY_STANDARD),
                ConfigValueOption(title="High (FLAC)", value=QUALITY_HIGH),
            ),
            description="Preferred format. The other format remains an automatic fallback.",
        ),
        ConfigEntry(
            key=CONF_LISTEN_PORT,
            type=ConfigEntryType.INTEGER,
            label="Soulseek listen port",
            default_value=DEFAULT_LISTEN_PORT,
            required=True,
            range=(1024, 65535),
            advanced=True,
            description="TCP listen port used by the embedded headless Nicotine+ core.",
        ),
        ConfigEntry(
            key=CONF_CACHE_LIMIT_GB,
            type=ConfigEntryType.INTEGER,
            label="Download cache limit (GB)",
            default_value=DEFAULT_CACHE_LIMIT_GB,
            required=True,
            range=(MIN_CACHE_LIMIT_GB, MAX_CACHE_LIMIT_GB),
            advanced=True,
            description="Maximum space for completed Soulseek downloads. Oldest "
            "completed tracks are removed first; active downloads are never evicted.",
        ),
    )


class SoulseekProvider(MusicProvider):
    """MusicBrainz metadata with peer-to-peer Soulseek playback."""

    _catalog: MusicBrainzClient
    _cache: DownloadCache
    _client: SoulseekClient

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """Return setup entries for Music Assistant 2.10 and newer."""

        return _build_config_entries()

    async def handle_async_init(self) -> None:
        self.logger.info("Soulseek provider version %s", __version__)
        quality = str(
            self.config.get_value(CONF_AUDIO_QUALITY) or QUALITY_STANDARD
        ).lower()
        if quality not in {QUALITY_STANDARD, QUALITY_HIGH}:
            raise InvalidDataError("Audio quality must be MP3 or FLAC")
        try:
            listen_port = int(
                self.config.get_value(CONF_LISTEN_PORT) or DEFAULT_LISTEN_PORT
            )
        except (TypeError, ValueError) as err:
            raise InvalidDataError("Soulseek listen port must be a number") from err
        if not 1024 <= listen_port <= 65535:
            raise InvalidDataError(
                "Soulseek listen port must be between 1024 and 65535"
            )
        try:
            cache_limit_gb = int(
                self.config.get_value(CONF_CACHE_LIMIT_GB) or DEFAULT_CACHE_LIMIT_GB
            )
        except (TypeError, ValueError) as err:
            raise InvalidDataError("Download cache limit must be a number") from err
        if not MIN_CACHE_LIMIT_GB <= cache_limit_gb <= MAX_CACHE_LIMIT_GB:
            raise InvalidDataError(
                f"Download cache limit must be between {MIN_CACHE_LIMIT_GB} and "
                f"{MAX_CACHE_LIMIT_GB} GB"
            )

        self._quality = quality
        self._cache = DownloadCache(
            Path(self.mass.storage_path) / "soulseek",
            cache_limit_gb * 1024**3,
        )
        await self._cache.initialize()
        self._catalog = MusicBrainzClient(self.mass, self.instance_id)
        self._client = SoulseekClient(
            loop=asyncio.get_running_loop(),
            cache=self._cache,
            listen_port=listen_port,
            logger=self.logger,
            on_download_complete=self._on_download_complete,
        )
        await self._client.start()

    async def unload(self, is_removed: bool = False) -> None:
        del is_removed
        if hasattr(self, "_client"):
            await self._client.stop()

    @property
    def supported_media_types(self) -> tuple[MediaType, ...]:
        return (MediaType.ARTIST, MediaType.ALBUM, MediaType.TRACK)

    async def search(
        self,
        search_query: str,
        media_types: list[MediaType] | None = None,
        limit: int = 5,
    ) -> SearchResults:
        result = await self._catalog.search(search_query, limit)
        requested = set(media_types or self.supported_media_types)
        return SearchResults(
            artists=(
                [self._artist(item) for item in result.artists[:limit]]
                if MediaType.ARTIST in requested
                else []
            ),
            albums=(
                [self._album(item) for item in result.albums[:limit]]
                if MediaType.ALBUM in requested
                else []
            ),
            tracks=(
                [self._track(item) for item in result.tracks[:limit]]
                if MediaType.TRACK in requested
                else []
            ),
        )

    async def get_artist(self, prov_artist_id: str) -> Artist:
        artist, _albums, _tracks = await self._catalog.artist_page(prov_artist_id)
        return self._artist(artist)

    async def get_artist_albums(self, prov_artist_id: str) -> list[Album]:
        _artist, albums, _tracks = await self._catalog.artist_page(prov_artist_id)
        return [self._album(item) for item in albums]

    async def get_artist_toptracks(self, prov_artist_id: str) -> list[Track]:
        _artist, _albums, tracks = await self._catalog.artist_page(prov_artist_id)
        return [self._track(item) for item in tracks]

    async def get_album(self, prov_album_id: str) -> Album:
        album, _tracks = await self._catalog.album_page(prov_album_id)
        return self._album(album)

    async def get_album_tracks(self, prov_album_id: str, page: int = 0) -> list[Track]:
        if page > 0:
            return []
        _album, tracks = await self._catalog.album_page(prov_album_id)
        return [self._track(item) for item in tracks]

    async def get_track(self, prov_track_id: str) -> Track:
        return self._track(await self._catalog.track_page(prov_track_id))

    def _mapping(self, item: MusicBrainzItem) -> set[ProviderMapping]:
        return {
            ProviderMapping(
                item_id=item.stable_id,
                provider_domain="soulseek",
                provider_instance=self.instance_id,
                url=item.url or None,
            )
        }

    def _images(self, item: MusicBrainzItem) -> UniqueList:
        if not item.artwork:
            return UniqueList()
        return UniqueList(
            [
                MediaItemImage(
                    type=ImageType.THUMB,
                    path=item.artwork,
                    provider=self.instance_id,
                    remotely_accessible=True,
                )
            ]
        )

    def _artist_mapping(self, item: MusicBrainzItem) -> ItemMapping:
        artist_id = (
            item.artist_id
            or f"name-{re.sub(r'[^a-z0-9]+', '-', item.artist_name.casefold())}"
        )
        return ItemMapping(
            media_type=MediaType.ARTIST,
            item_id=artist_id,
            provider=self.instance_id,
            name=item.artist_name or "Unknown Artist",
        )

    def _artist(self, item: MusicBrainzItem) -> Artist:
        artist = Artist(
            item_id=item.stable_id,
            provider=self.instance_id,
            name=item.name,
            provider_mappings=self._mapping(item),
        )
        artist.metadata.images = self._images(item)
        return artist

    def _album(self, item: MusicBrainzItem) -> Album:
        raw_type = item.album_type.casefold()
        album_type = {
            "album": AlbumType.ALBUM,
            "single": AlbumType.SINGLE,
            "ep": AlbumType.EP,
        }.get(raw_type, AlbumType.UNKNOWN)
        inferred = infer_album_type(item.name, item.album_type)
        if inferred in (AlbumType.SOUNDTRACK, AlbumType.LIVE):
            album_type = inferred
        album = Album(
            item_id=item.stable_id,
            provider=self.instance_id,
            name=item.name,
            artists=UniqueList([self._artist_mapping(item)]),
            year=item.year or 0,
            album_type=album_type,
            provider_mappings=self._mapping(item),
        )
        album.metadata.images = self._images(item)
        album.metadata.explicit = item.explicit
        return album

    def _track(self, item: MusicBrainzItem) -> Track:
        album = None
        if item.album_id:
            album = ItemMapping(
                media_type=MediaType.ALBUM,
                item_id=item.album_id,
                provider=self.instance_id,
                name=item.album_name,
                year=item.year,
            )
        track = Track(
            item_id=item.stable_id,
            provider=self.instance_id,
            name=item.name,
            artists=UniqueList([self._artist_mapping(item)]),
            album=album,
            duration=item.duration or 0,
            track_number=item.track_number,
            disc_number=item.disc_number,
            provider_mappings=self._mapping(item),
        )
        track.metadata.images = self._images(item)
        track.metadata.explicit = item.explicit
        return track

    async def get_stream_details(
        self, item_id: str, media_type: MediaType
    ) -> StreamDetails:
        if media_type != MediaType.TRACK:
            raise UnplayableMediaError("Soulseek playback is available for tracks only")
        cached = await self._cache.lookup(item_id, self._quality)
        if cached:
            return StreamDetails(
                provider=self.instance_id,
                item_id=item_id,
                audio_format=AudioFormat(
                    content_type=ContentType.try_parse(cached.extension),
                    bit_rate=cached.bitrate or None,
                ),
                stream_type=StreamType.LOCAL_FILE,
                path=cached.path,
                can_seek=True,
                allow_seek=True,
                duration=cached.duration,
            )

        track = await self._catalog.track_page(item_id)
        return StreamDetails(
            provider=self.instance_id,
            item_id=item_id,
            audio_format=AudioFormat(content_type=ContentType.UNKNOWN),
            stream_type=StreamType.CUSTOM,
            path="",
            can_seek=False,
            allow_seek=False,
            duration=track.duration,
            data={"track": asdict(track), "quality": self._quality},
        )

    async def get_audio_stream(
        self, streamdetails: StreamDetails, seek_position: int = 0
    ) -> AsyncGenerator[bytes, None]:
        """Protect the active track from LRU eviction while it is streaming."""

        await self._cache.set_active(streamdetails.item_id, True)
        try:
            async for chunk in self._get_uncached_audio_stream(
                streamdetails, seek_position
            ):
                yield chunk
        finally:
            await self._cache.set_active(streamdetails.item_id, False)

    async def _get_uncached_audio_stream(
        self, streamdetails: StreamDetails, seek_position: int = 0
    ) -> AsyncGenerator[bytes, None]:
        del seek_position
        data = streamdetails.data if isinstance(streamdetails.data, dict) else {}
        track_data = data.get("track")
        if not isinstance(track_data, dict):
            raise UnplayableMediaError("Soulseek stream metadata is missing")
        track = MusicBrainzItem(**track_data)
        variants = await self._client.search_variants(
            f"{track.artist_name} {track.name}".strip()
        )
        try:
            ranked = rank_variants(
                variants,
                artist=track.artist_name,
                title=track.name,
                catalog_duration=track.duration,
                prefer_flac=self._quality == QUALITY_HIGH,
            )
        except ValueError as err:
            raise UnplayableMediaError(str(err)) from err

        handle: DownloadHandle | None = None
        for peer_file in diversify_peers(ranked, limit=3):
            download_folder = await self._cache.prepare_download(
                track_id=streamdetails.item_id,
                profile=self._quality,
                duration=track.duration,
                bitrate=peer_file.bitrate,
            )
            try:
                candidate = await self._client.start_download(
                    peer_file,
                    folder=download_folder,
                    context={
                        "track_id": streamdetails.item_id,
                        "profile": self._quality,
                        "duration": track.duration,
                        "bitrate": peer_file.bitrate,
                    },
                )
            except Exception as err:  # noqa: BLE001 - one peer must not block failover
                self.logger.warning(
                    "Could not start Soulseek download from %s: %s",
                    peer_file.username,
                    err,
                )
                continue
            if await self._wait_for_first_bytes(candidate):
                handle = candidate
                break
            await self._client.abort_download(candidate)
        if handle is None:
            raise UnplayableMediaError(
                "No Soulseek peer delivered audio within 20 seconds"
            )

        file_handle: Any = None
        emitted = False
        last_progress = time.monotonic()
        last_size = -1
        try:
            while True:
                if file_handle is None:
                    path = Path(handle.path)
                    if path.is_file():
                        file_handle = await asyncio.to_thread(path.open, "rb")
                    else:
                        if handle.failed:
                            raise UnplayableMediaError(
                                f"Soulseek download failed before playback: {handle.error}"
                            )
                        await asyncio.sleep(0.1)
                        continue
                chunk = await asyncio.to_thread(file_handle.read, STREAM_CHUNK_SIZE)
                if chunk:
                    emitted = True
                    last_progress = time.monotonic()
                    yield chunk
                    continue

                if handle.finished:
                    return
                if handle.failed:
                    raise UnplayableMediaError(
                        f"Soulseek download stopped during playback: {handle.error}"
                    )
                with suppress(OSError):
                    current_size = Path(handle.path).stat().st_size
                    if current_size != last_size:
                        last_size = current_size
                        last_progress = time.monotonic()
                if time.monotonic() - last_progress >= MIDSTREAM_STALL_TIMEOUT:
                    # Once audio was emitted we deliberately do not switch files: doing so
                    # would splice two encodings or two different recordings together.
                    suffix = " after playback began" if emitted else ""
                    raise UnplayableMediaError(f"Soulseek transfer stalled{suffix}")
                handle.updated.clear()
                with suppress(TimeoutError):
                    await asyncio.wait_for(handle.updated.wait(), timeout=0.25)
        finally:
            if file_handle is not None:
                await asyncio.to_thread(file_handle.close)
            # Do not abort here: a selected transfer keeps downloading after a player
            # stops, so the completed file is available for a future local replay.

    async def _wait_for_first_bytes(self, handle: DownloadHandle) -> bool:
        deadline = time.monotonic() + FIRST_BYTE_TIMEOUT
        while time.monotonic() < deadline:
            if handle.failed:
                return False
            if handle.path:
                with suppress(OSError):
                    if Path(handle.path).stat().st_size > 0:
                        return True
            handle.updated.clear()
            with suppress(TimeoutError):
                await asyncio.wait_for(handle.updated.wait(), timeout=0.25)
        return False

    def _on_download_complete(self, handle: DownloadHandle) -> None:
        async def record() -> None:
            await self._cache.record(
                track_id=str(handle.context["track_id"]),
                profile=str(handle.context["profile"]),
                path=handle.path,
                duration=handle.context.get("duration"),
                bitrate=int(handle.context.get("bitrate") or 0),
            )

        asyncio.create_task(record())
