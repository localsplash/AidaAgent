FROM python:3.12-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 TZ=America/Los_Angeles
RUN apt-get update && apt-get install -y --no-install-recommends tzdata && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml README.md setup.py build_metadata.py MANIFEST.in ./
COPY src ./src
ARG BUILD_REVISION
ARG SOURCE_DATE_EPOCH
ARG BUILD_DIRTY
RUN pip install .

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
