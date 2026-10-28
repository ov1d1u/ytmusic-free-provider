"""MusicBrainz catalog adapter for the experimental Soulseek provider."""

from __future__ import annotations

import asyncio
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any, Literal

from music_assistant.controllers.cache import use_cache
from music_assistant_models.errors import InvalidDataError, MediaNotFoundError

MUSICBRAINZ_API_URL = "https://musicbrainz.org/ws/2"
MUSICBRAINZ_SITE_URL = "https://musicbrainz.org"
COVER_ART_URL = "https://coverartarchive.org/release-group"
REQUEST_TIMEOUT_SECONDS = 15
REQUEST_INTERVAL_SECONDS = 1.1
USER_AGENT = (
    "MusicAssistant-Soulseek/0.2.0 (https://github.com/sproft/ytmusic-free-provider)"
)


@dataclass(slots=True)
class MusicBrainzItem:
    """Normalized artist, release group, or recording."""

    kind: Literal["artist", "album", "track"]
    item_id: str
    name: str
    url: str = ""
    artist_name: str = ""
    artist_id: str = ""
    album_name: str = ""
    album_id: str = ""
    artwork: str = ""
    duration: int | None = None
    year: int | None = None
    explicit: bool = False
    track_number: int = 0
    disc_number: int = 0
    album_type: str = ""

    @property
    def stable_id(self) -> str:
        """Return the MusicBrainz identifier used by Music Assistant."""

        return self.item_id

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> MusicBrainzItem:
        return cls(**value)


@dataclass(slots=True)
class MusicBrainzSearch:
    artists: list[MusicBrainzItem]
    albums: list[MusicBrainzItem]
    tracks: list[MusicBrainzItem]

    def to_dict(self) -> dict[str, list[dict[str, Any]]]:
        return {
            "artists": [item.to_dict() for item in self.artists],
            "albums": [item.to_dict() for item in self.albums],
            "tracks": [item.to_dict() for item in self.tracks],
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> MusicBrainzSearch:
        return cls(
            artists=[MusicBrainzItem.from_dict(item) for item in value["artists"]],
            albums=[MusicBrainzItem.from_dict(item) for item in value["albums"]],
            tracks=[MusicBrainzItem.from_dict(item) for item in value["tracks"]],
        )


class MusicBrainzError(InvalidDataError):
    """MusicBrainz returned an unusable response."""


def _year(value: Any) -> int | None:
    text = str(value or "")
    if len(text) < 4 or not text[:4].isdigit():
        return None
    result = int(text[:4])
    if 1000 <= result <= datetime.now(UTC).year + 1:
        return result
    return None


def _duration(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        milliseconds = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if milliseconds <= 0:
        return None
    return max(1, round(milliseconds / 1000))


def _integer(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _artist_credit(value: Any) -> tuple[str, str]:
    if not isinstance(value, list):
        return "", ""
    display: list[str] = []
    first_id = ""
    for credit in value:
        if not isinstance(credit, dict):
            continue
        artist = credit.get("artist")
        if not isinstance(artist, dict):
            artist = {}
        name = str(credit.get("name") or artist.get("name") or "").strip()
        if name:
            display.append(name + str(credit.get("joinphrase") or ""))
        if not first_id:
            first_id = str(artist.get("id") or "")
    return "".join(display).strip(), first_id


def _cover_art(release_group_id: str) -> str:
    if not release_group_id:
        return ""
    return f"{COVER_ART_URL}/{release_group_id}/front-500"


def parse_artist(value: Any) -> MusicBrainzItem | None:
    if not isinstance(value, dict):
        return None
    item_id = str(value.get("id") or "")
    name = str(value.get("name") or "").strip()
    if not item_id or not name:
        return None
    return MusicBrainzItem(
        kind="artist",
        item_id=item_id,
        name=name,
        url=f"{MUSICBRAINZ_SITE_URL}/artist/{item_id}",
        artist_name=name,
        artist_id=item_id,
    )


def parse_release_group(value: Any) -> MusicBrainzItem | None:
    if not isinstance(value, dict):
        return None
    item_id = str(value.get("id") or "")
    name = str(value.get("title") or "").strip()
    if not item_id or not name:
        return None
    artist_name, artist_id = _artist_credit(value.get("artist-credit"))
    album_type = str(value.get("primary-type") or "")
    secondary = value.get("secondary-types")
    if isinstance(secondary, list) and secondary:
        album_type = str(secondary[0] or album_type)
    return MusicBrainzItem(
        kind="album",
        item_id=item_id,
        name=name,
        url=f"{MUSICBRAINZ_SITE_URL}/release-group/{item_id}",
        artist_name=artist_name,
        artist_id=artist_id,
        album_name=name,
        album_id=item_id,
        artwork=_cover_art(item_id),
        year=_year(value.get("first-release-date")),
        album_type=album_type,
    )


def _recording_album(value: dict[str, Any]) -> tuple[str, str, int | None]:
    releases = value.get("releases")
    if not isinstance(releases, list):
        return "", "", None
    releases = sorted(
        (release for release in releases if isinstance(release, dict)),
        key=lambda release: (
            str(release.get("status") or "").casefold() != "official",
            str(release.get("date") or "9999"),
            str(release.get("id") or ""),
        ),
    )
    for release in releases:
        group = release.get("release-group")
        if not isinstance(group, dict) or not group.get("id"):
            continue
        return (
            str(group["id"]),
            str(group.get("title") or release.get("title") or ""),
            _year(group.get("first-release-date") or release.get("date")),
        )
    return "", "", None


def parse_recording(
    value: Any,
    *,
    album: MusicBrainzItem | None = None,
    track_number: int = 0,
    disc_number: int = 0,
) -> MusicBrainzItem | None:
    if not isinstance(value, dict):
        return None
    item_id = str(value.get("id") or "")
    name = str(value.get("title") or "").strip()
    if not item_id or not name:
        return None
    artist_name, artist_id = _artist_credit(value.get("artist-credit"))
    album_id, album_name, year = _recording_album(value)
    if album:
        album_id = album.item_id
        album_name = album.name
        year = album.year
        artist_name = artist_name or album.artist_name
        artist_id = artist_id or album.artist_id
    return MusicBrainzItem(
        kind="track",
        item_id=item_id,
        name=name,
        url=f"{MUSICBRAINZ_SITE_URL}/recording/{item_id}",
        artist_name=artist_name,
        artist_id=artist_id,
        album_name=album_name,
        album_id=album_id,
        artwork=_cover_art(album_id),
        duration=_duration(value.get("length")),
        year=year,
        track_number=track_number,
        disc_number=disc_number,
    )


def parse_search_payloads(
    artists_payload: Any,
    albums_payload: Any,
    tracks_payload: Any,
) -> MusicBrainzSearch:
    """Normalize the three MusicBrainz search responses."""

    payloads = (
        (artists_payload, "artists", parse_artist),
        (albums_payload, "release-groups", parse_release_group),
        (tracks_payload, "recordings", parse_recording),
    )
    parsed: list[list[MusicBrainzItem]] = []
    for payload, key, parser in payloads:
        if not isinstance(payload, dict) or not isinstance(payload.get(key), list):
            raise MusicBrainzError(
                f"MusicBrainz search response did not contain a {key} list"
            )
        unique: dict[str, MusicBrainzItem] = {}
        for raw_item in payload[key]:
            if item := parser(raw_item):
                unique.setdefault(item.item_id, item)
        parsed.append(list(unique.values()))
    return MusicBrainzSearch(*parsed)


def parse_release_tracks(release: Any, album: MusicBrainzItem) -> list[MusicBrainzItem]:
    """Parse ordered recording rows from a MusicBrainz release lookup."""

    if not isinstance(release, dict) or not isinstance(release.get("media"), list):
        raise MusicBrainzError("MusicBrainz release response did not contain media")
    tracks: list[MusicBrainzItem] = []
    seen: set[str] = set()
    for medium_index, medium in enumerate(release["media"], 1):
        if not isinstance(medium, dict):
            continue
        disc_number = _integer(medium.get("position"), medium_index)
        medium_tracks = medium.get("tracks")
        if not isinstance(medium_tracks, list):
            continue
        for track_index, track in enumerate(medium_tracks, 1):
            if not isinstance(track, dict):
                continue
            recording = track.get("recording")
            if not isinstance(recording, dict):
                continue
            if not recording.get("artist-credit") and track.get("artist-credit"):
                recording = {**recording, "artist-credit": track["artist-credit"]}
            if recording.get("length") is None and track.get("length") is not None:
                recording = {**recording, "length": track["length"]}
            item = parse_recording(
                recording,
                album=album,
                track_number=_integer(track.get("position"), track_index),
                disc_number=disc_number,
            )
            if item and item.item_id not in seen:
                seen.add(item.item_id)
                tracks.append(item)
    return tracks


class MusicBrainzClient:
    """Fetch metadata from the credential-free MusicBrainz JSON API."""

    domain = "soulseek"

    def __init__(self, mass: Any, instance_id: str = "soulseek") -> None:
        self.mass = mass
        self.instance_id = instance_id
        self._request_lock = asyncio.Lock()
        self._last_request = 0.0

    async def _pace(self) -> None:
        async with self._request_lock:
            delay = REQUEST_INTERVAL_SECONDS - (time.monotonic() - self._last_request)
            if delay > 0:
                await asyncio.sleep(delay)
            self._last_request = time.monotonic()

    @staticmethod
    def _retry_delay(response: Any, attempt: int) -> float:
        raw = response.headers.get("Retry-After") if response.headers else None
        if raw:
            try:
                return max(REQUEST_INTERVAL_SECONDS, float(raw))
            except ValueError:
                try:
                    return max(
                        REQUEST_INTERVAL_SECONDS,
                        (
                            parsedate_to_datetime(raw) - datetime.now(UTC)
                        ).total_seconds(),
                    )
                except (TypeError, ValueError, OverflowError):
                    pass
        return REQUEST_INTERVAL_SECONDS * (attempt + 1)

    async def _request(self, entity: str, params: dict[str, Any]) -> dict[str, Any]:
        url = f"{MUSICBRAINZ_API_URL}/{entity}"
        request_params = {**params, "fmt": "json"}
        for attempt in range(3):
            await self._pace()
            try:
                async with self.mass.http_session.get(
                    url,
                    params=request_params,
                    headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
                    timeout=REQUEST_TIMEOUT_SECONDS,
                ) as response:
                    if response.status == 404:
                        raise MediaNotFoundError(
                            f"MusicBrainz entity {entity} was not found"
                        )
                    if response.status in (429, 503) and attempt < 2:
                        delay = self._retry_delay(response, attempt)
                    else:
                        response.raise_for_status()
                        payload = await response.json(content_type=None)
                        if not isinstance(payload, dict):
                            raise MusicBrainzError(
                                "MusicBrainz returned a non-object JSON response"
                            )
                        return payload
            except (MediaNotFoundError, MusicBrainzError):
                raise
            except Exception as err:
                if attempt >= 2:
                    raise MusicBrainzError(
                        f"Could not load MusicBrainz metadata: {err}"
                    ) from err
                delay = REQUEST_INTERVAL_SECONDS * (attempt + 1)
            await asyncio.sleep(delay)
        raise MusicBrainzError("MusicBrainz request failed after three attempts")

    @use_cache(expiration=900)
    async def search(self, query: str, limit: int = 25) -> MusicBrainzSearch:
        query = query.strip()
        if not query:
            return MusicBrainzSearch([], [], [])
        result_limit = min(max(1, limit), 100)
        artists, albums, tracks = await asyncio.gather(
            self._request("artist", {"query": query, "limit": result_limit}),
            self._request("release-group", {"query": query, "limit": result_limit}),
            self._request("recording", {"query": query, "limit": result_limit}),
        )
        return parse_search_payloads(artists, albums, tracks)

    async def _all_release_groups(self, artist_id: str) -> list[MusicBrainzItem]:
        results: dict[str, MusicBrainzItem] = {}
        offset = 0
        while True:
            payload = await self._request(
                "release-group",
                {
                    "artist": artist_id,
                    "inc": "artist-credits",
                    "limit": 100,
                    "offset": offset,
                },
            )
            values = payload.get("release-groups")
            if not isinstance(values, list):
                raise MusicBrainzError(
                    "MusicBrainz artist releases response did not contain release-groups"
                )
            for value in values:
                if item := parse_release_group(value):
                    results.setdefault(item.item_id, item)
            offset += len(values)
            count = _integer(payload.get("release-group-count"), offset)
            if not values or offset >= count:
                return list(results.values())

    @use_cache(expiration=86400, allow_expired_cache=True)
    async def artist_page(
        self, item_id: str
    ) -> tuple[MusicBrainzItem, list[MusicBrainzItem], list[MusicBrainzItem]]:
        artist_payload, albums, tracks_payload = await asyncio.gather(
            self._request(f"artist/{item_id}", {}),
            self._all_release_groups(item_id),
            self._request(
                "recording",
                {"query": f"arid:{item_id}", "limit": 100},
            ),
        )
        artist = parse_artist(artist_payload)
        if artist is None:
            raise MediaNotFoundError(f"MusicBrainz artist {item_id} was not found")
        raw_tracks = tracks_payload.get("recordings")
        if not isinstance(raw_tracks, list):
            raise MusicBrainzError(
                "MusicBrainz artist recordings response did not contain recordings"
            )
        tracks: dict[str, MusicBrainzItem] = {}
        for value in raw_tracks:
            if track := parse_recording(value):
                track.artist_name = track.artist_name or artist.name
                track.artist_id = track.artist_id or artist.item_id
                tracks.setdefault(track.item_id, track)
        return artist, albums, list(tracks.values())

    @staticmethod
    def _select_release(releases: Any) -> str:
        if not isinstance(releases, list):
            return ""
        choices = [value for value in releases if isinstance(value, dict)]
        choices.sort(
            key=lambda value: (
                str(value.get("status") or "").casefold() != "official",
                str(value.get("date") or "9999"),
                str(value.get("country") or ""),
                str(value.get("id") or ""),
            )
        )
        return str(choices[0].get("id") or "") if choices else ""

    @use_cache(expiration=86400, allow_expired_cache=True)
    async def album_page(
        self, item_id: str
    ) -> tuple[MusicBrainzItem, list[MusicBrainzItem]]:
        group_payload = await self._request(
            f"release-group/{item_id}",
            {"inc": "artist-credits+releases"},
        )
        album = parse_release_group(group_payload)
        if album is None:
            raise MediaNotFoundError(
                f"MusicBrainz release group {item_id} was not found"
            )
        release_id = self._select_release(group_payload.get("releases"))
        if not release_id:
            raise MediaNotFoundError(
                f"MusicBrainz release group {item_id} has no releases"
            )
        release = await self._request(
            f"release/{release_id}",
            {"inc": "recordings+artist-credits+release-groups+media"},
        )
        return album, parse_release_tracks(release, album)

    @use_cache(expiration=86400, allow_expired_cache=True)
    async def track_page(self, item_id: str) -> MusicBrainzItem:
        payload = await self._request(
            f"recording/{item_id}",
            {"inc": "artist-credits+releases+release-groups"},
        )
        track = parse_recording(payload)
        if track is None:
            raise MediaNotFoundError(f"MusicBrainz recording {item_id} was not found")
        return track
