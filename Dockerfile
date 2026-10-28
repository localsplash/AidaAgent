# Empty unless the deployment supplies LiveKitLogstore as the "livekitlogstore" build
# context (compose.yaml: the checkout of the tag in LIVEKIT_LOGSTORE_REF).
FROM scratch AS livekitlogstore

FROM python:3.12-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 TZ=America/Los_Angeles
RUN apt-get update && apt-get install -y --no-install-recommends tzdata && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml README.md setup.py build_metadata.py MANIFEST.in LIVEKIT_LOGSTORE_REF ./
COPY src ./src
ARG BUILD_REVISION
ARG SOURCE_DATE_EPOCH
ARG BUILD_DIRTY
# One resolve, so the library cannot pull a newer livekit-agents than the pin.
RUN --mount=from=livekitlogstore,target=/opt/livekit-logstore \
    if [ -f /opt/livekit-logstore/pyproject.toml ]; then pip install /opt/livekit-logstore .; \
    else pip install .; fi

FROM base AS test
ARG BUILD_REVISION
ARG SOURCE_DATE_EPOCH
ARG BUILD_DIRTY
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*
RUN pip install '.[test]'
COPY tests ./tests
RUN --network=none ruff check src tests setup.py build_metadata.py && pytest -q && aida-agent --help && aida-agent start --help && aida-agent preview --help

FROM base AS runtime
RUN useradd --create-home --uid 10001 aida
USER aida
RUN --network=none aida-agent start --help && aida-agent preview --help
ENTRYPOINT ["aida-agent"]
CMD ["start"]
