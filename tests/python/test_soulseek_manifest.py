"""Manifest and setup contract for the Soulseek provider."""

from __future__ import annotations

import json
from pathlib import Path

import soulseek

ROOT = Path(__file__).resolve().parents[2]


def test_soulseek_manifest_is_single_instance_and_pins_dependencies():
    manifest = json.loads(
        (ROOT / "soulseek" / "manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["domain"] == "soulseek"
    assert manifest["stage"] == "experimental"
    assert manifest["multi_instance"] is False
    assert manifest["requirements"] == ["nicotine-plus==3.3.11"]


def test_config_defaults_are_mp3_port_2234_without_disk_cache():
    entries = {entry.key: entry for entry in soulseek._build_config_entries()}
    assert entries[soulseek.CONF_AUDIO_QUALITY].default_value == "mp3"
    assert set(entries) == {
        soulseek.CONF_AUDIO_QUALITY,
        soulseek.CONF_LISTEN_PORT,
    }
    assert entries[soulseek.CONF_LISTEN_PORT].default_value == 2234
    assert entries[soulseek.CONF_LISTEN_PORT].advanced is True
    assert entries[soulseek.CONF_LISTEN_PORT].range == (1024, 65535)
