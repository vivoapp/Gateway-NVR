# Vivo Video API image. The base image is pinned by version and digest, and every Python package
# by version and hash (requirements.txt), so a rebuild gives the same software.
ARG PYTHON_IMAGE=python:3.12.16-slim-bookworm@sha256:935c3c5b12357a7e10caa9c1e6ca5692344d97cf229832ef584be2cec9fc03df

FROM ${PYTHON_IMAGE} AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir --require-hashes -r requirements.txt
COPY vivo_api ./vivo_api
USER 10001:10001
EXPOSE 8443
CMD ["python", "-m", "vivo_api.serve"]

# Unit tests in the same image: docker build --target test -t vivo-video-api:test . && docker run --rm vivo-video-api:test
FROM runtime AS test
USER root
RUN pip install --no-cache-dir pytest==9.2.0
COPY tools ./tools
COPY gateway ./gateway
COPY tests ./tests
COPY docs ./docs
USER 10001:10001
CMD ["python", "-m", "pytest", "-q", "-p", "no:warnings", "-p", "no:cacheprovider", "tests/unit"]
