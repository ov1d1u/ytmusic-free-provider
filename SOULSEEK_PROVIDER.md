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
state, generated Soulseek credentials, incomplete transfers, and completed
downloads are persisted in `./data` by default.

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
- **Download cache limit** defaults to **5 GB**. It accepts values from 1 to
  1000 GB and removes the least-recently accessed completed tracks first.

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
├── cache_index.json
├── credentials.json
├── downloads/
├── incomplete/
└── nicotine/
```

Completed files form a persistent, size-bounded LRU cache. Successful cache
access updates a track's recency, and the provider enforces the configured limit
at startup and after each completed transfer. Active and incomplete downloads
are protected from eviction. The atomic cache index is reconciled with files at
startup; missing files are dropped, while older rows gain an access timestamp
from their file modification time. Shares are configured as empty and upload
slots are disabled: downloaded files are not offered back to Soulseek peers.

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

On a cache miss, the provider tries the top three peers. Each gets 20 seconds to
produce the first bytes. Failover is allowed only before audio is emitted. Once
playback begins, the provider never splices another file into the stream; a
later stall fails cleanly and replay performs a fresh resolution. Downloads
continue after playback stops, and completed files replay as seekable local
files.

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
- **Cache evicts tracks sooner than expected:** raise the advanced download
  cache limit. Only completed, inactive tracks are eligible for LRU eviction.
- **A track starts then stops:** the selected peer stalled. The provider will
  not switch files midstream; replay the track to resolve it again.

## Manual end-to-end check

1. Search for an artist and open its albums and recordings.
2. Start an uncached track and confirm playback begins while the file grows in
   `soulseek/incomplete` or `soulseek/downloads`.
3. Let it finish, replay it, and confirm it is served as a seekable local file.
4. Restart Music Assistant and replay again to verify the persistent cache.
5. Confirm the completed file does not appear in the configured Soulseek shares.

Live MusicBrainz compatibility is checked only as an advisory canary. Live
Soulseek behavior is deliberately excluded from deterministic CI because peer
availability and network conditions cannot produce repeatable results.
