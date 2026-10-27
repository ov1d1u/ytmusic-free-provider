#!/usr/bin/env python3
"""Inspect YouTube Music's optional song/video links without changing playback.

Requires Python 3.10+ and ytmusicapi (``python3 -m pip install ytmusicapi``).
See https://ytmusicapi.readthedocs.io/en/stable/reference/watch.html

Examples, run from the repository root:
    python3 scripts/inspect_counterparts.py --video-id 9mWr4c_ig54
    python3 scripts/inspect_counterparts.py --playlist-id YOUR_MIX_ID --auth browser.json
    python3 scripts/inspect_counterparts.py --video-id YOUR_VIDEO_ID --json

Use IDs from the watch URL's ``v=`` or ``list=`` parameter. Personal mixes
usually need browser authentication; create its headers file with
``ytmusicapi browser``. This is a headers JSON file, not a raw Cookie string.
``limit`` is a minimum for the API; this script displays at most that many items.
OMVs without a typed ATV counterpart trigger an artist/title song search.
Search matches are shown separately and do not change provider playback.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from typing import Any


VIDEO_TYPES = {
    "MUSIC_VIDEO_TYPE_ATV": "album audio / Art Track",
    "MUSIC_VIDEO_TYPE_OMV": "official music video",
    "MUSIC_VIDEO_TYPE_UGC": "user upload",
}
ATV = "MUSIC_VIDEO_TYPE_ATV"
OMV = "MUSIC_VIDEO_TYPE_OMV"
SEARCH_LIMIT = 10
VERIFY_LIMIT = 5


def text_key(text: str) -> str:
    """Compare punctuation and accents consistently without dropping version names."""
    text = unicodedata.normalize("NFKD", text.casefold())
    return "".join(char for char in text if char.isalnum())


def song_title(title: str) -> str:
    """Remove video labels, keeping meaningful qualifiers such as live or remix."""
    label = r"(?:official\s+)?(?:music\s+)?video(?:\s+(?:hd|4k))?"
    title = re.sub(rf"\(\s*{label}\s*\)|\[\s*{label}\s*\]", "", title, flags=re.I)
    return re.sub(rf"\s*[-–—|:]\s*{label}\s*$", "", title, flags=re.I).strip()


def search_album_audio(client: Any, track: dict[str, Any]) -> dict[str, Any] | None:
    """Search only OMVs lacking a typed audio counterpart; never invent a link.

    Search results often omit videoType. In that case, ask for the candidate's
    watch queue and check the entry with that exact ID before calling it ATV.
    Search matches are candidates, not proof of YouTube's song/video pairing.
    """
    counterpart = track.get("counterpart") or {}
    if track.get("videoType") != OMV or (
        counterpart.get("videoType") == ATV and counterpart.get("videoId")
    ):
        return None
    title = song_title(track.get("title") or "")
    artists = track.get("artists") or []
    names = {text_key(artist.get("name") or "") for artist in artists} - {""}
    artist_ids = {artist["id"] for artist in artists if artist.get("id")}
    if not title or not names:
        return {"status": "skipped", "reason": "Missing song title or artist name"}
    query = " ".join([artists[0].get("name") or "", title]).strip()
    report: dict[str, Any] = {"query": query, "status": "not_found"}
    try:
        results = client.search(query, filter="songs", limit=SEARCH_LIMIT)
    except Exception as err:
        return {**report, "status": "error", "error": f"{type(err).__name__}: {err}"}

    checked: set[str] = set()
    for candidate in results[:SEARCH_LIMIT]:
        video_id = candidate.get("videoId")
        if not video_id or video_id == track.get("videoId") or video_id in checked:
            continue
        if candidate.get("isAvailable") is False:
            continue
        if text_key(song_title(candidate.get("title") or "")) != text_key(title):
            continue
        candidate_artists = candidate.get("artists") or []
        same_artist = any(
            artist.get("id") in artist_ids or text_key(artist.get("name") or "") in names
            for artist in candidate_artists
        )
        if not same_artist:
            continue
        checked.add(video_id)
        video_type = candidate.get("videoType")
        if not video_type:
            try:
                queue = client.get_watch_playlist(videoId=video_id, limit=1)
                entry = next(
                    (item for item in queue.get("tracks") or [] if item.get("videoId") == video_id),
                    {},
                )
                video_type = entry.get("videoType")
            except Exception as err:
                report.setdefault("verification_errors", []).append(
                    {"videoId": video_id, "error": f"{type(err).__name__}: {err}"}
                )
        if video_type == ATV:
            return {**report, "status": "found", "match": {**candidate, "videoType": ATV}}
        if len(checked) >= VERIFY_LIMIT:
            break
    return report


def describe(track: dict[str, Any], label: str) -> None:
    """Print metadata as returned, without inferring type from title or artwork."""
    print(f"  {label}: {track.get('title') or '(no title)'}")
    artists = ", ".join(artist.get("name") or "?" for artist in track.get("artists") or [])
    print(f"    Artists: {artists or '(not returned)'}")
    video_type = track.get("videoType")
    print(
        f"    Type: {video_type or '(not returned)'}"
        f" — {VIDEO_TYPES.get(video_type, 'unknown')}"
    )
    album = track.get("album") or {}
    print(f"    Album: {album.get('name') or '(not returned)'}")
    print(f"    Duration: {track.get('length') or track.get('duration') or '(not returned)'}")
    video_id = track.get("videoId")
    print(f"    ID: {video_id or '(not returned)'}")
    if video_id:
        print(f"    URL: https://music.youtube.com/watch?v={video_id}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--video-id", help="A song or video ID (not the full URL)")
    target.add_argument("--playlist-id", help="A playlist or mix ID, including personal mixes")
    parser.add_argument("--auth", help="Optional ytmusicapi browser headers JSON file")
    parser.add_argument("--limit", type=int, default=10, help="Maximum items displayed (default: 10)")
    parser.add_argument("--json", action="store_true", help="Print tracks and audio_search reports as JSON")
    args = parser.parse_args()
    if args.limit < 1:
        parser.error("--limit must be greater than zero")

    try:
        from ytmusicapi import YTMusic
    except ImportError:
        print("Install the dependency first: python3 -m pip install ytmusicapi", file=sys.stderr)
        return 1

    try:
        client = YTMusic(auth=args.auth)
        # Fetch the watch queue, which carries the optional counterpart object.
        # get_playlist() and extracting an audio stream are different operations.
        response = client.get_watch_playlist(
            videoId=args.video_id,
            playlistId=args.playlist_id,
            limit=args.limit,
        )
    except Exception as err:
        print(f"YouTube Music request failed ({type(err).__name__}): {err}", file=sys.stderr)
        return 1

    tracks = (response.get("tracks") or [])[:args.limit]
    if not tracks:
        print("YouTube returned no tracks. Personal mixes may require --auth.", file=sys.stderr)
        return 1
    # Keep the API's counterpart intact. Search results go in a separate field.
    tracks = [dict(track) for track in tracks]
    for track in tracks:
        if (report := search_album_audio(client, track)) is not None:
            track["audio_search"] = report
    if args.json:
        print(json.dumps(tracks, indent=2, ensure_ascii=False))
        return 0

    print("counterpart is optional and may point to a video rather than album audio.")
    print("A missing counterpart does not prove that no album version exists.")
    print("OMVs without an ATV counterpart get an artist/title search for album audio.")
    print("Search matches are candidates, not linked counterparts; playback is unchanged.")
    linked = 0
    for index, track in enumerate(tracks, 1):
        print(f"\n{index}.")
        describe(track, "Original")
        counterpart = track.get("counterpart")
        if not isinstance(counterpart, dict) or not counterpart.get("videoId"):
            print("  Counterpart: not returned")
        else:
            linked += 1
            describe(counterpart, "Counterpart")
        if track.get("videoType") == ATV:
            print("  Album audio is already the original; swapping blindly would be wrong.")
        elif isinstance(counterpart, dict) and counterpart.get("videoType") == ATV:
            print("  The counterpart is marked as album audio: a possible replacement.")
        if report := track.get("audio_search"):
            print(f"  Audio search: {report.get('query') or report.get('reason')}")
            if report["status"] == "found":
                describe(report["match"], "Search match (not a linked counterpart)")
            elif report["status"] == "error":
                print(f"    Search failed: {report['error']}")
            elif report["status"] == "not_found":
                print("    No verified ATV match with the same title and artist was found.")
            for error in report.get("verification_errors", []):
                print(f"    Type lookup failed for {error['videoId']}: {error['error']}")
    print(f"\n{linked}/{len(tracks)} displayed tracks have a linked counterpart.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
