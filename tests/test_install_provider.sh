#!/bin/sh
# Tests for scripts/install_provider.sh
#
# Run from the repo root:   sh tests/test_install_provider.sh
# Or as a CI step.
#
# install_provider.sh requires the host Docker daemon to copy the provider into
# the running Music Assistant container, so there is no offline end-to-end path
# the way there is for the watcher installer. These tests therefore cover the
# script structure, the usage/error paths, and the Docker-missing guidance that
# HAOS users hit from the sandboxed Terminal & SSH add-on (issue #11).

set -u

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SCRIPT="$REPO_ROOT/scripts/install_provider.sh"
SOULSEEK_SCRIPT="$REPO_ROOT/scripts/install_soulseek_provider.sh"

PASS=0
FAIL=0
SKIP=0

red()    { printf '\033[31m%s\033[0m' "$*"; }
green()  { printf '\033[32m%s\033[0m' "$*"; }
yellow() { printf '\033[33m%s\033[0m' "$*"; }

pass() { PASS=$((PASS+1)); printf '  %s %s\n' "$(green PASS)" "$1"; }
fail() { FAIL=$((FAIL+1)); printf '  %s %s\n' "$(red   FAIL)" "$1"; [ -n "${2:-}" ] && printf '       %s\n' "$2"; }
skip() { SKIP=$((SKIP+1)); printf '  %s %s\n' "$(yellow SKIP)" "$1"; }

assert_eq() {
    # assert_eq <name> <expected> <actual>
    if [ "$2" = "$3" ]; then
        pass "$1"
    else
        fail "$1" "expected: $2 / actual: $3"
    fi
}

assert_contains() {
    # assert_contains <name> <needle> <haystack>
    case "$3" in
        *"$2"*) pass "$1" ;;
        *)      fail "$1" "expected to contain: $2" ;;
    esac
}

assert_file_exists() {
    # assert_file_exists <name> <path>
    if [ -f "$2" ]; then
        pass "$1"
    else
        fail "$1" "missing file: $2"
    fi
}

# --- Section 1: script structure / preflight (no network) -------------------

printf '\n== Script structure ==\n'

assert_file_exists "installer script exists" "$SCRIPT"
assert_file_exists "Soulseek convenience installer exists" "$SOULSEEK_SCRIPT"

shebang="$(head -n1 "$SCRIPT")"
assert_eq "shebang is POSIX sh" "#!/bin/sh" "$shebang"

if sh -n "$SCRIPT" 2>/dev/null; then
    pass "POSIX sh -n syntax check"
else
    fail "POSIX sh -n syntax check"
fi

if sh -n "$SOULSEEK_SCRIPT" 2>/dev/null; then
    pass "Soulseek wrapper POSIX sh -n syntax check"
else
    fail "Soulseek wrapper POSIX sh -n syntax check"
fi

# Bashisms guard (best-effort; not exhaustive). Skips comment lines.
bashisms="$(grep -nE '\[\[|^[[:space:]]*local |<<<|\$\{[A-Za-z_]+,,\}|\$\{[A-Za-z_]+\^\^\}' "$SCRIPT" \
            | grep -v '^[[:space:]]*#' || true)"
if [ -z "$bashisms" ]; then
    pass "no obvious bashisms in installer"
else
    fail "no obvious bashisms in installer" "$bashisms"
fi

# --- MA container auto-detect regex (issue #35) -----------------------------

printf '\n== MA container auto-detect regex ==\n'

# Beta/nightly/dev MA installs name their container e.g.
# "addon_d5369777_music_assistant_beta"; auto-detect must match those, not only
# the stable "..._music_assistant" name (issue #35). Extract the exact pattern
# the script uses so this test tracks the real code, then exercise it (this also
# proves the ERE is portable to the dash/BusyBox grep the CI runs under).
MA_PAT="$(sed -n "s/^MA_NAME_RE='\(.*\)'$/\1/p" "$SCRIPT" | head -n1)"

if [ -n "$MA_PAT" ]; then
    pass "extracted MA-detect regex from script"
    assert_contains "MA-detect regex allows a channel suffix" "music_assistant(" "$MA_PAT"
    for _name in addon_d5369777_music_assistant \
                 addon_d5369777_music_assistant_beta \
                 addon_ff_music_assistant_nightly \
                 addon_ff_music_assistant_dev \
                 app_d5369777_music_assistant \
                 app_d5369777_music_assistant_beta \
                 app_ff_music_assistant_nightly \
                 app_ff_music_assistant_dev; do
        if printf '%s\n' "$_name" | grep -qE "$MA_PAT"; then
            pass "MA-detect regex matches $_name"
        else
            fail "MA-detect regex matches $_name" "expected a match"
        fi
    done
    # The watcher's own container must never match, or the installer targets
    # itself. Both spellings, since both are now accepted on the left.
    for _name in addon_ff_ma_provider_watcher \
                 app_ff_ma_provider_watcher \
                 addon_ff_music_assistant_watcher \
                 app_ff_music_assistant_watcher \
                 addon_ff_some_music_assistant_x \
                 app_ff_some_music_assistant_x \
                 apps_ff_music_assistant \
                 myapp_ff_music_assistant \
                 music_assistant; do
        if printf '%s\n' "$_name" | grep -qE "$MA_PAT"; then
            fail "MA-detect regex rejects $_name" "unexpected match"
        else
            pass "MA-detect regex rejects $_name"
        fi
    done
else
    fail "extracted MA-detect regex from script" "could not find the grep -E '^addon_...' line"
fi

# Recovery hints must use the pipe-safe "sh -s --" form: the documented install
# is "curl ... | sh", where a bare "--flag" is parsed by sh itself and fails
# with "sh: bad option" (issue #35). Guard the fix against regressing.
src="$(cat "$SCRIPT")"
assert_contains "re-run hints use the sh -s -- separator" "sh -s --" "$src"
assert_contains "installer supports the Soulseek provider" "soulseek)" "$src"
assert_contains "Soulseek dependency is exactly pinned" \
    "nicotine-plus==3.3.11" "$src"

# --- Release-tracking install (issue #68) ------------------------------------
#
# Installing a published release rather than branch head is what makes an
# install reproducible and lets a bug report name a version.

assert_contains "resolves the newest release when --ref is absent" \
    "latest_release_tag()" "$src"
assert_contains "falls back rather than aborting when there is no release" \
    'REF="main"' "$src"
# The bare tar.gz form resolves a branch, a tag and a commit identically.
# refs/heads/ 404s on every tag, which is what made --ref <tag> unusable.
# shellcheck disable=SC2016  # matching the literal text "$REF" in the source
assert_contains "downloads with the ref-agnostic tarball form" \
    'tar.gz/$REF' "$src"
case "$src" in
    *'tar.gz/refs/heads/'*) fail "does not hardcode refs/heads in the tarball URL" ;;
    *) pass "does not hardcode refs/heads in the tarball URL" ;;
esac
# GitHub strips a leading "v" from tag names in the archive's top-level
# directory, so v1.0.0 extracts to "<repo>-1.0.0". Computing the name from the
# ref misses by one character and the install dies with "not found in archive".
# shellcheck disable=SC2016  # matching the literal old expression in the source
case "$src" in
    *'SRC_ROOT="$TMPDIR/$REPO_NAME-$SAFE_REF"'*)
        fail "discovers the extracted directory instead of computing it" ;;
    *) pass "discovers the extracted directory instead of computing it" ;;
esac
assert_contains "help explains the release default" "newest published release" "$src"
case "$src" in
    *"then re-run with --ma-id ID"*) fail "no bare 're-run with --ma-id ID' hint remains" ;;
    *) pass "no bare 're-run with --ma-id ID' hint remains" ;;
esac
case "$src" in
    *"sh install_provider.sh --force --ma-id"*) fail "no bare 'sh install_provider.sh --force --ma-id' hint remains" ;;
    *) pass "no bare 'sh install_provider.sh --force --ma-id' hint remains" ;;
esac

printf '\n== Usage / error paths ==\n'

help_out="$(sh "$SCRIPT" --help 2>&1)"; help_rc=$?
assert_eq "--help exits 0" "0" "$help_rc"
assert_contains "--help prints Usage:" "Usage:" "$help_out"
assert_contains "--help mentions --force" "--force" "$help_out"
assert_contains "--help mentions --provider" "--provider" "$help_out"
assert_contains "--help mentions --ref" "--ref" "$help_out"
assert_contains "--help mentions --ma-id" "--ma-id" "$help_out"
assert_contains "--help mentions --python-version" "--python-version" "$help_out"
assert_contains "--help mentions --no-restart" "--no-restart" "$help_out"
assert_contains "--help mentions --no-stage" "--no-stage" "$help_out"

bad_out="$(sh "$SCRIPT" --bogus-option 2>&1)"; bad_rc=$?
if [ "$bad_rc" -ne 0 ]; then
    pass "unknown option exits non-zero"
else
    fail "unknown option exits non-zero" "exit code was 0"
fi
assert_contains "unknown option mentions the option" "--bogus-option" "$bad_out"

# --- Section 2: Docker-missing guidance (issue #11) -------------------------
#
# Build a sandbox PATH that contains the tools the preflight needs BEFORE the
# docker check, but deliberately omits 'docker'. The script must then abort with
# actionable guidance rather than a bare "command not found".

printf '\n== Docker-missing guidance ==\n'

SH_BIN="$(command -v sh)"
SANDBOX_BIN="$(mktemp -d 2>/dev/null || mktemp -d -t mipt)"

sandbox_ok=1
for cmd in date curl tar mkdir cp rm; do
    src="$(command -v "$cmd" 2>/dev/null || true)"
    if [ -z "$src" ]; then
        sandbox_ok=0
        break
    fi
    ln -s "$src" "$SANDBOX_BIN/$cmd" 2>/dev/null \
        || cp "$src" "$SANDBOX_BIN/$cmd" 2>/dev/null \
        || sandbox_ok=0
done

if [ "$sandbox_ok" = "1" ] && [ ! -e "$SANDBOX_BIN/docker" ]; then
    nodocker_out="$(PATH="$SANDBOX_BIN" "$SH_BIN" "$SCRIPT" --force 2>&1)"; nodocker_rc=$?
    if [ "$nodocker_rc" -ne 0 ]; then
        pass "missing docker exits non-zero"
    else
        fail "missing docker exits non-zero" "exit code was 0"
    fi
    assert_contains "missing docker mentions docker"          "docker"                   "$nodocker_out"
    assert_contains "missing docker points at Advanced SSH"   "Advanced SSH"             "$nodocker_out"
    assert_contains "missing docker mentions Protection mode" "Protection mode"          "$nodocker_out"
    assert_contains "missing docker offers watcher fallback"  "install_watcher_addon.sh" "$nodocker_out"
else
    skip "could not build a docker-free sandbox PATH on this system"
fi

rm -rf "$SANDBOX_BIN"

# --- Summary ----------------------------------------------------------------

printf '\n== Summary ==\n'
printf '  passed:  %s\n' "$PASS"
printf '  failed:  %s\n' "$FAIL"
printf '  skipped: %s\n' "$SKIP"

if [ "$FAIL" -gt 0 ]; then
    exit 1
fi
exit 0
