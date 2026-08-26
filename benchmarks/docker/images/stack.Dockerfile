# Shared environment for both stacks. genropy is INSTALLED IN THE IMAGE: it is
# the pinned baseline of every comparison and its build drags five sibling
# directories (~340 MB), too heavy for a container start. Rebuild the image
# when the baseline tree moves: docker compose build legacy bridge.
# The fast-moving packages (genro-asgi, genropy-asgi) are NOT here: they are
# installed at container start from their read-only mounts, so the running
# commit is always the mounted tree's.
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpq5 libpq-dev gcc curl \
    && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
COPY --from=genropy gnrpy /build/genropy/gnrpy
COPY --from=genropy gnrjs /build/genropy/gnrjs
COPY --from=genropy projects /build/genropy/projects
COPY --from=genropy dojo_libs /build/genropy/dojo_libs
COPY --from=genropy resources /build/genropy/resources
COPY --from=genropy webtools /build/genropy/webtools
RUN uv pip install --system /build/genropy/gnrpy psycopg2-binary && rm -rf /build
# cryptography's embedded OpenSSL probes ARM crypto extensions the Docker
# Desktop VM does not expose and dies of SIGILL; armcap=0 disables the probe.
ENV OPENSSL_armcap=0
ENV GENRO_GNRFOLDER=/lab/gnr
WORKDIR /lab
