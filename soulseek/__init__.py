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

from .client import DownloadHandle, SoulseekClient
from .musicbrainz import MusicBrainzClient, MusicBrainzItem
from .ranking import diversify_peers, rank_variants
from .storage import RuntimeStorage

if TYPE_CHECKING:
    from music_assistant import MusicAssistant
    from music_assistant.models import ProviderInstanceType
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest


CONF_AUDIO_QUALITY = "audio_quality"
CONF_LISTEN_PORT = "listen_port"
QUALITY_STANDARD = "mp3"
QUALITY_HIGH = "flac"
DEFAULT_LISTEN_PORT = 2234
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
    )


class SoulseekProvider(MusicProvider):
    """MusicBrainz metadata with peer-to-peer Soulseek playback."""

    _catalog: MusicBrainzClient
    _storage: RuntimeStorage
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
        self._quality = quality
        self._storage = RuntimeStorage(Path(self.mass.storage_path) / "soulseek")
        await self._storage.initialize()
        self._catalog = MusicBrainzClient(self.mass, self.instance_id)
        self._client = SoulseekClient(
            loop=asyncio.get_running_loop(),
            storage=self._storage,
            listen_port=listen_port,
            logger=self.logger,
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
        """Yield incoming audio directly from memory as soon as it arrives."""
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
        try:
            for peer_file in diversify_peers(ranked, limit=3):
                try:
                    handle = await self._client.start_download(peer_file)
                except Exception as err:  # noqa: BLE001 - allow peer failover
                    self.logger.warning(
                        "Could not start Soulseek transfer from %s: %s",
                        peer_file.username,
                        err,
                    )
                    continue
                if await self._wait_for_first_bytes(handle):
                    break
                await self._client.abort_download(handle)
                handle = None
            if handle is None:
                raise UnplayableMediaError("No Soulseek peer delivered audio within 20 seconds")

            offset = 0
            last_progress = time.monotonic()
            while True:
                # Clear before inspecting state so a network write cannot be lost
                # between the read and wait. Bytes wake playback immediately.
                handle.updated.clear()
                chunk = handle.buffer.read(offset, STREAM_CHUNK_SIZE)
                if chunk:
                    offset += len(chunk)
                    last_progress = time.monotonic()
                    yield chunk
                    continue
                if handle.failed:
                    raise UnplayableMediaError(
                        f"Soulseek transfer stopped during playback: {handle.error}"
                    )
                if handle.finished:
                    return
                remaining = MIDSTREAM_STALL_TIMEOUT - (time.monotonic() - last_progress)
                if remaining <= 0:
                    raise UnplayableMediaError("Soulseek transfer stalled after playback began")
                with suppress(TimeoutError):
                    await asyncio.wait_for(handle.updated.wait(), timeout=remaining)
        finally:
            if handle is not None:
                await self._client.abort_download(handle)

    async def _wait_for_first_bytes(self, handle: DownloadHandle) -> bool:
        deadline = time.monotonic() + FIRST_BYTE_TIMEOUT
        while True:
            handle.updated.clear()
            if handle.failed:
                return False
            if handle.buffer is not None and len(handle.buffer):
                return True
            remaining = deadline - time.monotonic()
            if handle.finished or remaining <= 0:
                return False
            with suppress(TimeoutError):
                await asyncio.wait_for(handle.updated.wait(), timeout=remaining)
