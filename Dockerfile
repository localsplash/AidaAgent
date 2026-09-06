FROM python:3.12-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install .

FROM base AS test
RUN pip install '.[test]'
COPY tests ./tests
RUN --network=none ruff check src tests && pytest -q && aida-agent --help && aida-agent start --help

FROM base AS runtime
RUN useradd --create-home --uid 10001 aida
USER aida
RUN --network=none aida-agent start --help
ENTRYPOINT ["aida-agent"]
CMD ["start"]
