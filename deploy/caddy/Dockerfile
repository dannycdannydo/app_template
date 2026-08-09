# Edge reverse proxy for the hybrid VPS profile (Scope §6.6, blueprint §35.1).
#
# Stock Caddy does not ship a rate-limiting directive, so this image is built
# with xcaddy, pinning both Caddy itself (v2.11.4) and the rate-limiting
# module (mholt/caddy-ratelimit v0.1.0). Pinning both makes the edge rate
# limiter reproducible and testable: CI builds this image and runs
# `caddy validate` against deploy/caddy/Caddyfile (SECURITY.md — edge rate
# limiting, Scope §6.6).
#
# Upgrade procedure: bump CADDY_VERSION and RATELIMIT_MODULE together, rebuild
# this image, and re-run the CI edge validation job before deploying.

FROM caddy:2.11.4-builder AS builder

ARG CADDY_VERSION=v2.11.4
ARG RATELIMIT_MODULE=github.com/mholt/caddy-ratelimit@v0.1.0

RUN xcaddy build ${CADDY_VERSION} --with ${RATELIMIT_MODULE}

FROM caddy:2.11.4

COPY --from=builder /usr/bin/caddy /usr/bin/caddy
