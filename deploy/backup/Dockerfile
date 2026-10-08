# Off-site backup job for the hybrid VPS profile (ADR-0023,
# docs/backup-and-recovery.md → Off-site backup job).
#
# PostgreSQL client tools for `pg_dump`/`pg_restore` plus rclone for the
# encrypted upload to an S3-compatible bucket at a second provider. Both base
# images are pinned by digest; rclone is copied from its official image.
#
# `pg_dump` must be the same major version as the server or newer. The client
# is PostgreSQL 17 to match the template's development and tested-restore
# database; bump POSTGRES_CLIENT_PACKAGE (and the alpine pin if the package is
# not in that release) when a deployment's managed PostgreSQL is newer.
#
# Upgrade procedure: bump the pinned tags and digests together, rebuild, and
# re-run the CI off-site backup validation job before deploying.

FROM rclone/rclone:1.75.1@sha256:45401ad7410db1d67ffdb58e19059ad20b0d8e0285a60e38bbec55cc1019c7a5 AS rclone

FROM alpine:3.24.2@sha256:294b683cb724975bec92580e1e685676bd4b50bda910ddb8c51d4cabeaec77e6

ARG POSTGRES_CLIENT_PACKAGE=postgresql17-client

RUN apk add --no-cache bash ca-certificates "${POSTGRES_CLIENT_PACKAGE}" \
    && addgroup -S backup \
    && adduser -S -G backup -h /var/lib/offsite-backup backup

COPY --from=rclone /usr/local/bin/rclone /usr/local/bin/rclone
COPY offsite-backup.sh /usr/local/bin/offsite-backup

USER backup
WORKDIR /var/lib/offsite-backup

ENTRYPOINT ["offsite-backup"]
CMD ["schedule"]
