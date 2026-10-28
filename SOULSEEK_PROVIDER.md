# Experimental Soulseek Provider

This provider lets Music Assistant browse artists, release groups, and
recordings from MusicBrainz, then finds a matching FLAC or MP3 file through
Soulseek. It is experimental and is not affiliated with MusicBrainz, MetaBrainz,
Soulseek, Nicotine+, Sonosano, or Music Assistant.

## Installation

The repository's Docker image includes both `ytmusic_free` and `soulseek`. For a
Home Assistant add-on installation, run the dedicated installer from a checkout:

```sh
sh scripts/install_soulseek_provider.sh --force
```

Or use the portable installer directly:

```sh
curl -fsSL https://raw.githubusercontent.com/sproft/ytmusic-free-provider/main/scripts/install_provider.sh \
  | sh -s -- --provider soulseek --force
```

After Music Assistant restarts, open **Settings → Music providers → Add** and
select **Soulseek**. Only one instance is supported because Nicotine+ owns a
process-wide networking core.

The provider pins `nicotine-plus==3.3.11`. Nicotine+ remains an external
GPL-3.0-or-later dependency; its source is not copied into this MIT repository.
The provider artwork and integration approach credit the Apache-2.0 Sonosano
project in `soulseek/NOTICE`.

### Docker Compose

The repository includes a stack that builds the custom Music Assistant image
and runs it with both providers installed:

```sh
docker compose up -d --build
```

Open `http://HOST_IP:8095`, then add **Soulseek** under Music providers. Runtime
state and generated Soulseek credentials are persisted in `./data` by default.
Audio bytes stay in memory and are released when playback ends.

Optional settings can be placed in `.env`:

```sh
cp .env.example .env
```

- `MA_VERSION` selects the upstream Music Assistant base-image channel and
  defaults to `latest`.
- `MASS_DATA_DIR` changes the host directory mounted at `/data`.
- `LOG_LEVEL` defaults to `info`.

Use `docker compose build --pull` when you want to refresh the upstream Music
Assistant base image, followed by `docker compose up -d`. Do not point stable
and beta versions at the same data directory.

The stack intentionally uses host networking. Music Assistant needs direct LAN
access for discovery and streaming, and Soulseek needs its configured listen
port reachable on the host. On Linux, allow TCP `8095`, TCP `8097`, and the
configured Soulseek port (TCP `2234` by default) through the host firewall.
Additional player providers may require more ports. Host networking must be
enabled separately on Docker Desktop, and Docker Desktop may not provide the
same layer-2 discovery behavior as a native Linux host.

The stack does not grant broad filesystem capabilities. Prefer mounting local
or host-mounted network music into the container read-only. Only add
`SYS_ADMIN`, `DAC_READ_SEARCH`, and `apparmor:unconfined` if Music Assistant
must mount SMB/NFS shares from inside the container and you accept the reduced
isolation.

## Settings

- **Audio quality** defaults to **Standard (MP3)**. **High (FLAC)** prefers
  FLAC, but each mode can fall back to the other format when necessary.
- **Soulseek listen port** defaults to TCP `2234`.

MusicBrainz needs no API key. Artist IDs are MusicBrainz artist MBIDs, album IDs
are release-group MBIDs, and track IDs are recording MBIDs. The client identifies
itself with a meaningful User-Agent and spaces requests to respect the public
API's one-request-per-second rate limit. Search results are cached briefly and
catalog drill-downs longer.

No Soulseek account is required. A random username and password are generated
and stored only after the first successful login in Music Assistant's persistent
storage.

## Networking and storage

Allow outbound HTTPS access to `musicbrainz.org` and `coverartarchive.org`, plus
outbound access to the Soulseek network. For better peer connectivity, forward
the configured TCP listen port from your router and firewall to the Music
Assistant host/container. A closed port can reduce the set of reachable peers,
although searches may still work.

Persistent files live below Music Assistant's storage directory:

```text
soulseek/
├── credentials.json
└── nicotine/
```

Audio is received only into memory, with a 256 MiB limit per transfer. Playback
can read the first bytes immediately while the peer is still sending the rest.
Transfers larger than the limit fail cleanly. Shares are empty and upload slots
are disabled. Audio is never offered back to peers.

The provider no longer reads or creates an audio cache. Existing `downloads/`,
`incomplete/`, and `cache_index.json` files from older versions are left untouched
and can be removed manually. The old download cache limit setting is ignored.

## Matching and playback

MusicBrainz's recording length is the main defense against remixes and
mislabeled files. The provider keeps the closest result and variants within two
additional seconds, then applies the selected format preference. When
MusicBrainz has no length, it chooses the largest group of peer files whose
durations fit within a two-second span and targets that group's median.
Filename/artist/title relevance resolves likely remixes and mislabeled files.
Within that relevance group, a free upload slot, a shorter queue, and higher
advertised upload speed are preferred before bitrate, peer, and path resolve
remaining ties deterministically. Missing peer metrics remain valid candidates
but never outrank known favorable values solely because data is absent.

The provider tries the top three peers. Each gets 20 seconds to
produce the first bytes. Failover is allowed only before audio is emitted. Once
playback begins, the provider never splices another file into the stream; a
later stall fails cleanly and replay performs a fresh resolution. Stopping
playback cancels the transfer and releases its audio buffer. Concurrent players
using the same peer file share a transfer until the last player stops. Streams
are not seekable and completed audio is not retained for replay.

## Catalog behavior

Search queries MusicBrainz artists, release groups, and recordings. Artist album
drill-down browses all release groups. Since MusicBrainz is a community metadata
database rather than a popularity service, the provider's **top tracks** view
shows up to 100 recordings ordered by MusicBrainz search relevance; it is not a
play-count chart. Opening an album selects an official release where possible,
then uses that release's media and track ordering.

Cover images use the Cover Art Archive's release-group front-image endpoint.
Some artists, recordings, and releases have incomplete metadata or no cover;
those fields remain empty instead of being guessed from a different catalog.

## Legal and privacy notice

Soulseek is a peer-to-peer network. Availability does not imply that a file is
licensed for you to download. Laws and rights differ by country; use the
provider only for material you are legally permitted to access. Searches expose
the generated Soulseek username and your network address to the service and
peers in the ordinary operation of the protocol.

MusicBrainz metadata is provided by the public MusicBrainz web service. It is a
community-maintained database and may contain omissions or errors. Cover images
are requested from the separate Cover Art Archive service.

## Troubleshooting

- **Provider will not start:** confirm the configured port is between 1024 and
  65535 and not already owned by another process. Only one instance may run.
- **Search is slow:** the provider deliberately limits MusicBrainz requests to
  about one per second. One search spans three catalog entity types; cached
  repeats are faster.
- **Search works but playback does not:** allow outbound Soulseek traffic and
  forward the listen port. Try a well-known track with several peers.
- **MusicBrainz error:** confirm `musicbrainz.org` is reachable and wait before
  retrying. HTTP 429 and 503 responses are retried with bounded backoff.
- **A track starts then stops:** the selected peer stalled. The provider will
  not switch files midstream; replay the track to resolve it again.

## Manual end-to-end check

1. Search for an artist and open its albums and recordings.
2. Start a track and confirm playback begins before the transfer finishes.
3. Confirm no audio files appear in persistent storage during playback.
4. Stop playback and confirm the transfer stops and its buffer is released.
5. Replay the track and confirm it resolves a peer again.

Live MusicBrainz compatibility is checked only as an advisory canary. Live
Soulseek behavior is deliberately excluded from deterministic CI because peer
availability and network conditions cannot produce repeatable results.
