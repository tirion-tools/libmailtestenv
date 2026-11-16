# Gate toolchain for tools/gate.sh's static jobs: shellcheck, shfmt and Python 3 (standard library only).
# Base pinned by digest (debian:trixie); packages from the Debian snapshot of 2026-11-12 at exact versions
# (apt checks them against the signed snapshot indexes).
FROM debian@sha256:9c6e381421d0f48d1b8133e573f33c185ee16cbf505eda9c4a8a4791b25ea8a6
RUN rm -f /etc/apt/sources.list.d/debian.sources \
    && echo 'deb [check-valid-until=no] http://snapshot.debian.org/archive/debian/20261112T000000Z trixie main' \
       > /etc/apt/sources.list.d/snapshot.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends shellcheck=0.10.0-1 shfmt=3.8.0-1+b8 python3=3.13.5-1 \
    && rm -rf /var/lib/apt/lists/*
