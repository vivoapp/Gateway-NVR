# One image for the uploader and the fake recorder; a second stage adds the tests.
FROM python:3.12-slim-bookworm AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    SEGMENT_UPLOADER_CONFIG=/app/config.docker.toml

# Fixed non-root user, so files in the shared volumes are not owned by root.
RUN useradd --uid 1000 --create-home --shell /bin/false uploader

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY segment_uploader/ segment_uploader/
COPY tools/ tools/
COPY docker/config.docker.toml /app/config.docker.toml

RUN mkdir -p /data/recordings /data/state && chown -R 1000:1000 /data /app
USER 1000:1000

CMD ["python", "-m", "segment_uploader", "run", "--config", "/app/config.docker.toml"]


# `docker compose --profile test run --rm tests` builds and runs this stage.
FROM runtime AS test
USER root
COPY requirements-dev.txt pytest.ini ./
RUN pip install --no-cache-dir -r requirements-dev.txt
COPY tests/ tests/
RUN chown -R 1000:1000 /app
USER 1000:1000
CMD ["pytest"]
