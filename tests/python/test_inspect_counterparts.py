"""Offline checks for the sample script's song matching and type verification."""

from __future__ import annotations

import contextlib
import copy
import importlib.util
import io
import json
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import Mock, patch


SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "inspect_counterparts.py"
SPEC = importlib.util.spec_from_file_location("inspect_counterparts", SCRIPT)
sample = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sample)

VIDEO = {
    "videoId": "Ahha3Cqe_fk",
    "title": "The One That Got Away (Official Music Video)",
    "artists": [{"name": "Katy Perry", "id": "katy"}],
    "videoType": sample.OMV,
}
SONG = {
    "videoId": "audio123456",
    "title": "The One That Got Away",
    "artists": [{"name": "Katy Perry", "id": "katy"}],
    "resultType": "song",
    "duration": "3:47",
}


class CounterpartSearchTests(unittest.TestCase):
    def test_untyped_song_requires_verification_of_its_own_id(self):
        client = Mock()
        client.search.return_value = [SONG]
        client.get_watch_playlist.return_value = {
            "tracks": [{"videoId": SONG["videoId"], "videoType": sample.ATV}]
        }
        report = sample.search_album_audio(client, VIDEO)
        self.assertEqual(report["status"], "found")
        self.assertEqual(report["match"]["videoType"], sample.ATV)
        self.assertNotIn("videoType", SONG)  # Do not modify the API's objects.
        client.search.assert_called_once_with(
            "Katy Perry The One That Got Away", filter="songs", limit=10
        )
        client.get_watch_playlist.assert_called_once_with(videoId=SONG["videoId"], limit=1)

    def test_typed_atv_needs_no_extra_lookup(self):
        client = Mock()
        client.search.return_value = [{**SONG, "videoType": sample.ATV}]
        self.assertEqual(sample.search_album_audio(client, VIDEO)["status"], "found")
        client.get_watch_playlist.assert_not_called()

    def test_skips_covers_live_versions_unavailable_tracks_and_videos(self):
        client = Mock()
        client.search.return_value = [
            {**SONG, "artists": [{"name": "Cover artist"}], "videoType": sample.ATV},
            {**SONG, "title": "The One That Got Away (Live)", "videoType": sample.ATV},
            {**SONG, "isAvailable": False, "videoType": sample.ATV},
            {**SONG, "videoType": sample.OMV},
            {**SONG, "videoId": "right123456", "videoType": sample.ATV},
        ]
        report = sample.search_album_audio(client, VIDEO)
        self.assertEqual(report["match"]["videoId"], "right123456")
        client.get_watch_playlist.assert_not_called()

    def test_unrelated_queue_entry_does_not_confirm_audio(self):
        client = Mock()
        client.search.return_value = [SONG]
        client.get_watch_playlist.return_value = {
            "tracks": [{"videoId": "differentID", "videoType": sample.ATV}]
        }
        self.assertEqual(sample.search_album_audio(client, VIDEO)["status"], "not_found")

    def test_search_only_for_omv_without_audio_counterpart(self):
        client = Mock()
        for track in (
            {**VIDEO, "videoType": sample.ATV},
            {**VIDEO, "videoType": "MUSIC_VIDEO_TYPE_UGC"},
            {**VIDEO, "counterpart": {"videoId": SONG["videoId"], "videoType": sample.ATV}},
        ):
            self.assertIsNone(sample.search_album_audio(client, track))
        client.search.assert_not_called()
        # A counterpart with unknown type still needs a search.
        client.search.return_value = []
        sample.search_album_audio(client, {**VIDEO, "counterpart": {"videoId": "unknown"}})
        client.search.assert_called_once()

    def test_failed_search_and_type_lookup_are_reported(self):
        client = Mock()
        client.search.side_effect = RuntimeError("search failed")
        self.assertEqual(sample.search_album_audio(client, VIDEO)["status"], "error")
        client.search.side_effect = None
        client.search.return_value = [SONG]
        client.get_watch_playlist.side_effect = RuntimeError("lookup failed")
        report = sample.search_album_audio(client, VIDEO)
        self.assertEqual(report["status"], "not_found")
        self.assertIn("lookup failed", report["verification_errors"][0]["error"])

    def test_json_preserves_original_and_labels_search_match_separately(self):
        original = copy.deepcopy(VIDEO)
        client = Mock()
        client.get_watch_playlist.return_value = {"tracks": [original]}
        client.search.return_value = [{**SONG, "videoType": sample.ATV}]
        module = types.SimpleNamespace(YTMusic=Mock(return_value=client))
        output = io.StringIO()
        with (
            patch.dict(sys.modules, {"ytmusicapi": module}),
            patch.object(sys, "argv", [str(SCRIPT), "--video-id", VIDEO["videoId"], "--json"]),
            contextlib.redirect_stdout(output),
        ):
            self.assertEqual(sample.main(), 0)
        result = json.loads(output.getvalue())[0]
        self.assertEqual(result["videoId"], VIDEO["videoId"])
        self.assertNotIn("counterpart", result)
        self.assertEqual(result["audio_search"]["match"]["videoId"], SONG["videoId"])
        self.assertEqual(original, VIDEO)


if __name__ == "__main__":
    unittest.main()
