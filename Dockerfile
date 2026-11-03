# Development image: the Python services plus their pinned dev dependencies.
#
# This is deliberately not the appliance image (that is `os/`, built with mkosi).
# It exists so a normal Docker host can run and test the services with no
# Bluetooth, no WiFi, no root and no appliance hardware.
FROM python:3.11.9-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app/services/engine/src:/app/services/provisioning/src:/app/services/led/src

WORKDIR /app

COPY requirements-dev.txt requirements-docker.txt ./
RUN apt-get update \
    && apt-get install -y --no-install-recommends make \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir -r requirements-docker.txt

COPY . .

# /state is the engine's state root; creating it here (before the volume mount)
# makes a named volume inherit the ownership of an unprivileged user.
RUN useradd --create-home --uid 10001 openpc \
    && mkdir -p /state/agent \
    && chown -R openpc:openpc /app /state

USER openpc

EXPOSE 8095 8090

CMD ["python", "-m", "openpc_engine", "--help"]
