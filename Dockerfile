# Base image version (moving tags: latest, beta, nightly)
# Declared before the first FROM so it is usable in the FROM line below.
ARG MA_VERSION=latest

# --- Stage: runtime -----------------------------------------------------------
FROM ghcr.io/music-assistant/server:${MA_VERSION} AS runtime

# Add OCI labels for basic image introspection
LABEL org.opencontainers.image.source="https://github.com/sproft/ytmusic-free-provider" \
      org.opencontainers.image.description="Unofficial build of the upstream server image with the ytmusic_free and soulseek providers pre-installed. Independent community project, not affiliated with or endorsed by the upstream project."

# Copy the provider directory from the build context into the base image and
# detect the active Python version to move files to the correct site-packages
# folder.
COPY ytmusic_free/ /tmp/ytmusic_free
COPY soulseek/ /tmp/soulseek
# Resolve the site-packages path via Python itself (no globbing, no
# hard-coded version fallback): if the venv Python is broken, the build
# fails here instead of silently installing to the wrong location. Only
# Nicotine+'s headless networking core is used; --no-deps avoids pulling its
# GTK/PyGObject desktop dependencies into the server image.
RUN PROVIDERS_DIR="$( /app/venv/bin/python -c 'import sysconfig; print(sysconfig.get_path("purelib"))' )/music_assistant/providers" && \
    rm -rf "$PROVIDERS_DIR/ytmusic_free" "$PROVIDERS_DIR/soulseek" && \
    mv /tmp/ytmusic_free "$PROVIDERS_DIR/ytmusic_free" && \
    mv /tmp/soulseek "$PROVIDERS_DIR/soulseek" && \
    /app/venv/bin/uv pip install --python /app/venv/bin/python --no-cache --no-deps \
        "nicotine-plus==3.3.11"
