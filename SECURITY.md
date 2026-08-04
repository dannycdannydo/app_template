# Security

The practical baseline is **OWASP ASVS Level 2**. This file records the controls the template enforces and the expectations for applications built from it. Deferred capabilities (auth, storage, jobs, audit) arrive in later template releases; the controls below state the target for the final system.

## Baseline controls

- WorkOS session validation for all authenticated routes.
- Default-deny authorisation; permissions are explicit, never implicit.
- Tenant-scoped queries; cross-tenant access is a bug.
- Explicit CORS allowlist, no wildcard origins in production.
- CSRF protection where cookie authentication requires it.
- Input limits on request bodies, string lengths, and upload sizes.
- Rate limiting on public and sensitive endpoints.
- Secure headers (HSTS, CSP, frame, MIME sniffing, referrer) at the edge.
- Private object storage; no public buckets.
- Upload scanning hook on all uploaded content.
- Restricted external URL fetching (SSRF controls, see below).
- Webhook signature verification for inbound webhooks.
- Non-public PostgreSQL and Redis; no exposed database ports.
- Least-privilege database credentials per role.
- Encrypted backups; off-site copies.
- Secret scanning, dependency scanning, and container scanning in CI.
- Non-root containers.
- Safe error messages: the standard API error format never leaks internals, stack traces, or secrets (see `API_CONVENTIONS.md`).

## File security

Uploaded files are untrusted. Controls include:

- MIME and extension validation;
- page and size limits;
- decompression-bomb protections;
- worker isolation for processing;
- no execution of uploaded content;
- a quarantine state;
- a malware scanning hook.

## SSRF

User-supplied URLs must not access:

- localhost;
- loopback addresses;
- private network ranges;
- cloud metadata endpoints;
- internal service names.

## Administrative access

Cross-organisation support or impersonation must be:

- explicit;
- limited;
- visible;
- fully audited.

There is no hidden universal bypass.

## Secrets

- No secrets are committed to the repository.
- `.env.example` documents every variable the application reads, with safe placeholder values; real secrets live in environment-specific secret stores.
- Never log secrets, tokens, or credentials.

## Reporting a vulnerability

If you find a security issue, report it privately to the maintainers before disclosing publicly. Include a description of the issue, affected versions, and a minimal reproduction if possible. Do not open a public issue for security vulnerabilities.

## Deferred controls

The following controls land with their owning capabilities in later releases and must be present before v1.0: WorkOS session validation (v0.2), tenant isolation enforcement (v0.2), storage/file controls (v0.4), audit logging (v0.5), and rate limiting at the edge for production profiles.
