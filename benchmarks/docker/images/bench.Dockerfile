# The measuring container: stdlib-only drivers, so bare python is enough.
# The bench scripts arrive by mount from the genropy-asgi tree.
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /src/genropy-asgi/benchmarks
