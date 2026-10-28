FROM python:3.12-slim-bookworm@sha256:0f5b26b9518d002b6173fd61daad821fa340635ebfec5bba471013f9ca114579
RUN apt-get update && apt-get install -y --no-install-recommends gcc g++ libc6-dev \
    && rm -rf /var/lib/apt/lists/*
USER 65534:65534
WORKDIR /tmp
# This image is an execution toolchain, not an authoritative scoring runner.
# The fixed backend injects its trusted driver and overrides the entrypoint.
ENTRYPOINT ["/usr/local/bin/python3", "-c", "raise SystemExit('fixed_stdio_backend_required')"]
