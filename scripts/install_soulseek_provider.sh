#!/bin/sh
# Dedicated entry point for installing the experimental Soulseek provider.

set -eu

SCRIPT_DIR=$(CDPATH= cd -P "$(dirname "$0")" 2>/dev/null && pwd) || SCRIPT_DIR=""
if [ -n "$SCRIPT_DIR" ] && [ -f "$SCRIPT_DIR/install_provider.sh" ]; then
    exec sh "$SCRIPT_DIR/install_provider.sh" --provider soulseek "$@"
fi

INSTALLER_URL="https://raw.githubusercontent.com/sproft/ytmusic-free-provider/main/scripts/install_provider.sh"
TMP_INSTALLER="$(mktemp 2>/dev/null || mktemp -t soulseek-installer)"
trap 'rm -f "$TMP_INSTALLER"' EXIT INT TERM
curl -fsSL "$INSTALLER_URL" -o "$TMP_INSTALLER"
sh "$TMP_INSTALLER" --provider soulseek "$@"
