# ADR 0023: Opt-in Off-site Backup Job with rclone

Status: Accepted (2026-10-08, human-reviewed: backup and recovery, infrastructure and secret handling)

## Context

The hybrid VPS profile (ADR-0007) keeps durable state in managed PostgreSQL
and S3-compatible object storage. `docs/backup-and-recovery.md` relies on the
provider's PITR and bucket versioning, and also requires a nightly logical dump
"sent off-site" with a backup-failure alert, but the template shipped no
mechanism for it. Provider-native backups live in the same account as the
data, so they do not protect against the failures most likely for a small
client deployment: a lost, locked or compromised provider account, an unpaid
bill, or a deletion by someone holding the account's credentials. Some
low-cost providers (for example DigitalOcean Spaces) also lack cross-region
replication and object lock.

Requirements: hands-off once configured; nothing runs unless an off-site
destination is configured; data encrypted before it leaves the host; the dump
taken with the `BYPASSRLS` operational credential (ADR-0022 decision 4) so no
row is silently omitted; a restore path that does not depend on hand-built
tooling; failures visible to monitoring.

## Options considered

1. **Host cron + scripts outside Compose** (rejected). Not deployed by the
   release workflow, invisible to `docker compose ps`, and every host would
   need its own tooling installed and kept current.
2. **A job in the backend image (Dramatiq maintenance task)** (rejected). The
   backend image has no `pg_dump`, the job would load the `BYPASSRLS`
   credential into the application runtime (which ADR-0022 forbids), and an
   off-site copy must not depend on the application being healthy.
3. **Provider-specific replication** (rejected). Not available on every
   provider, ties the template to one vendor, and stays within one account.
4. **A dedicated Compose service with PostgreSQL client tools and rclone**
   (adopted).

## Decision

- Add an `offsite-backup` service to `compose.hybrid-vps.yml`, built from
  `deploy/backup/` (pinned `alpine` base, `postgresql17-client`, rclone copied
  from the pinned official `rclone/rclone` image). The deploy workflow builds,
  publishes and pulls it like the Caddy edge image.
- **Opt-in by configuration.** The job runs only when `BACKUP_S3_ENDPOINT`,
  `BACKUP_S3_BUCKET`, `BACKUP_S3_ACCESS_KEY_ID` and
  `BACKUP_S3_SECRET_ACCESS_KEY` are all set. With none set it reports itself
  disabled and idles (healthy). A partial configuration, or a missing
  `BACKUP_ENCRYPTION_PASSWORD`/`DATABASE_OPERATOR_URL`/source storage setting,
  is a configuration error: the container exits non-zero and restarts.
- **Content.** Nightly at `BACKUP_HOUR_UTC`: `pg_dump --format=custom` with
  `DATABASE_OPERATOR_URL`, verified with `pg_restore --list` before upload,
  plus `rclone copy` of the `STORAGE_BUCKET` objects (excluding the transient
  `staging/` and `ai/scratch/` prefixes) unless `BACKUP_INCLUDE_FILES=false`.
- **Encryption.** rclone crypt with `BACKUP_ENCRYPTION_PASSWORD` encrypts
  contents and object names on the host. A symmetric passphrase was chosen
  over public-key encryption (e.g. age) because rclone crypt gives incremental
  copies of the file bucket natively; anyone holding the host already holds the
  live database and storage credentials, so a host-held key adds little
  exposure.
- **Never delete off-site.** Dump names are unique and the file copy never
  removes destination objects, so the destination key needs no delete
  permission; retention is a provider-side lifecycle rule (plus object lock
  where available).
- **Least privilege on the host.** The service receives only its named
  variables (not the whole environment file), joins its own `backup` network
  (no Redis, no edge), publishes no port and runs as a non-root user. Secrets
  reach `pg_dump` and rclone through the environment, never a command line.
- **Monitoring.** An optional `BACKUP_HEARTBEAT_URL` dead-man's switch is
  requested after each successful run; the container healthcheck fails when an
  enabled job's last success is older than 26 hours.
- **Restore tooling ships in the same image:** `list`, `fetch-dump` (decrypt
  and verify one dump) and `restore-files`.

## Dependency

rclone (MIT licence, maintained, wide S3-compatible provider coverage) is a
new deployment-image dependency. It is not used by the backend or frontend.
It replaces what would otherwise be a hand-written S3 client plus encryption
and incremental-sync logic.

## Consequences

- An off-site backup becomes a configuration step per deployment rather than
  bespoke work; deployments that do not configure it are unaffected.
- `pg_dump` must be at least the server's major version. The image pins the
  PostgreSQL 17 client; deployments on a newer managed PostgreSQL must bump
  `POSTGRES_CLIENT_PACKAGE`, or the nightly run fails (and alerts).
- Losing `BACKUP_ENCRYPTION_PASSWORD` makes the off-site data unrecoverable; it
  must be stored with the `.env.production` off-site copy.
- The RPO of the off-site path is up to 24 hours; provider PITR remains the
  primary restore path.
