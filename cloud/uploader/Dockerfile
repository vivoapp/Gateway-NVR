# One image for the uploader and the fake recorder; a second stage adds the tests.
# The base image is pinned by digest. Update tag and digest together, never the tag alone.
FROM python:3.12-slim-bookworm@sha256:62ed8e07e544e8555c5d93748d5275015136b7f5c52b5e8679b4de339c3779d3 AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    SEGMENT_UPLOADER_CONFIG=/app/config.docker.toml

# Fixed non-root user (10001 on the gateway), so files in the shared volumes are not owned by root.
RUN useradd --uid 10001 --create-home --shell /bin/false uploader

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY segment_uploader/ segment_uploader/
COPY tools/ tools/
COPY docker/config.docker.toml /app/config.docker.toml

RUN mkdir -p /data/recordings /data/state /data/recovery && chown -R 10001:10001 /data /app
USER 10001:10001

CMD ["python", "-m", "segment_uploader", "run", "--config", "/app/config.docker.toml"]


# `docker compose --profile test run --rm tests` builds and runs this stage.
FROM runtime AS test
USER root
COPY requirements-dev.txt pytest.ini ./
RUN pip install --no-cache-dir -r requirements-dev.txt
COPY tests/ tests/
RUN chown -R 10001:10001 /app
USER 10001:10001
CMD ["pytest"]
