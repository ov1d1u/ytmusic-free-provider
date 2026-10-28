"""Deterministic selection of playable Soulseek search results."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import PurePosixPath
from statistics import median
from typing import Any

PLAYABLE_EXTENSIONS = frozenset({"flac", "mp3"})


@dataclass(slots=True, frozen=True)
class PeerFile:
    """One remote file returned by a Soulseek peer."""

    username: str
    path: str
    size: int = 0
    extension: str = ""
    bitrate: int = 0
    duration: float | None = None
    file_attributes: Any = field(default_factory=dict, compare=False)
    free_upload_slots: bool | None = None
    queue_size: int | None = None
    upload_speed: int | None = None

    @property
    def normalized_extension(self) -> str:
        value = (
            self.extension or PurePosixPath(self.path.replace("\\", "/")).suffix
        ).lower()
        return value.removeprefix(".")


def _normalized_words(value: str) -> str:
    value = re.sub(r"\.[^.]+$", "", value.casefold())
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return " ".join(value.split())


def _filename_relevance(candidate: PeerFile, artist: str, title: str) -> float:
    filename = PurePosixPath(candidate.path.replace("\\", "/")).name
    haystack = _normalized_words(filename)
    wanted = _normalized_words(f"{artist} {title}")
    if not wanted or not haystack:
        return 0.0
    wanted_words = set(wanted.split())
    overlap = len(wanted_words.intersection(haystack.split())) / len(wanted_words)
    sequence = SequenceMatcher(None, wanted, haystack).ratio()
    return (overlap * 0.7) + (sequence * 0.3)


def deduplicate_variants(variants: Iterable[PeerFile]) -> list[PeerFile]:
    """Remove duplicate peer/path/size rows while preserving deterministic order."""

    unique: dict[tuple[str, str, int], PeerFile] = {}
    for candidate in variants:
        if candidate.normalized_extension not in PLAYABLE_EXTENSIONS:
            continue
        key = (candidate.username.casefold(), candidate.path, candidate.size)
        unique.setdefault(key, candidate)
    return list(unique.values())


def _densest_duration_cluster(variants: list[PeerFile]) -> list[PeerFile]:
    timed = sorted(
        (item for item in variants if item.duration is not None and item.duration > 0),
        key=lambda item: (
            float(item.duration or 0),
            item.username.casefold(),
            item.path,
        ),
    )
    if not timed:
        return variants

    best: list[PeerFile] = []
    left = 0
    for right, _item in enumerate(timed):
        while float(_item.duration or 0) - float(timed[left].duration or 0) > 2:
            left += 1
        cluster = timed[left : right + 1]
        if not best:
            best = cluster
            continue
        cluster_peers = len({item.username.casefold() for item in cluster})
        best_peers = len({item.username.casefold() for item in best})
        cluster_spread = float(cluster[-1].duration or 0) - float(
            cluster[0].duration or 0
        )
        best_spread = float(best[-1].duration or 0) - float(best[0].duration or 0)
        cluster_key = (len(cluster), cluster_peers, -cluster_spread)
        best_key = (len(best), best_peers, -best_spread)
        if cluster_key > best_key:
            best = cluster
    return best


def rank_variants(
    variants: Iterable[PeerFile],
    *,
    artist: str,
    title: str,
    catalog_duration: float | None,
    prefer_flac: bool,
) -> list[PeerFile]:
    """Rank playable files by recording match, format, then peer availability."""

    playable = deduplicate_variants(variants)
    if not playable:
        raise ValueError("No FLAC or MP3 files were returned by Soulseek")

    timed = [
        item for item in playable if item.duration is not None and item.duration > 0
    ]
    target_duration: float | None = None
    selected = playable
    if catalog_duration is not None and float(catalog_duration) > 0 and timed:
        target_duration = float(catalog_duration)
        closest = min(
            abs(float(item.duration or 0) - target_duration) for item in timed
        )
        selected = [
            item
            for item in timed
            if abs(float(item.duration or 0) - target_duration) <= closest + 2
        ]
    elif timed:
        selected = _densest_duration_cluster(playable)
        target_duration = float(median(float(item.duration or 0) for item in selected))

    extension_order = {"flac": 0, "mp3": 1} if prefer_flac else {"mp3": 0, "flac": 1}

    def sort_key(item: PeerFile) -> tuple[float | int | str, ...]:
        duration_delta = (
            abs(float(item.duration or 0) - target_duration)
            if target_duration is not None and item.duration is not None
            else 0.0
        )
        if item.free_upload_slots is True:
            slot_order = 0
        elif item.free_upload_slots is None:
            slot_order = 1
        else:
            slot_order = 2
        queue_order = (
            max(item.queue_size, 0)
            if item.queue_size is not None
            else 2**32
        )
        return (
            extension_order[item.normalized_extension],
            -_filename_relevance(item, artist, title),
            slot_order,
            queue_order,
            -max(item.upload_speed or 0, 0),
            -max(item.bitrate, 0),
            duration_delta,
            item.username.casefold(),
            item.path.casefold(),
            item.size,
        )

    return sorted(selected, key=sort_key)


def diversify_peers(ranked: Iterable[PeerFile], limit: int = 3) -> list[PeerFile]:
    """Choose failover candidates from distinct peers before repeating one."""

    values = list(ranked)
    selected: list[PeerFile] = []
    seen: set[str] = set()
    for item in values:
        peer = item.username.casefold()
        if peer in seen:
            continue
        seen.add(peer)
        selected.append(item)
        if len(selected) >= limit:
            return selected
    for item in values:
        if item in selected:
            continue
        selected.append(item)
        if len(selected) >= limit:
            break
    return selected
