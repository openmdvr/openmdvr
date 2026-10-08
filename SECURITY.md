# Security Policy

OpenMDVR handles location history, live video and remote vehicle commands
(including engine cut-off). Security reports are our top priority.

## Reporting a vulnerability

**Please do not open a public issue.** Report privately through
[GitHub Security Advisories](../../security/advisories/new)
("Report a vulnerability" on the Security tab).

Include, if you can:
- affected component (`jt808-server`, `api`, `web`, `infra`) and commit,
- steps to reproduce or a proof of concept,
- impact (e.g. cross-tenant data access, auth bypass, DoS of device ingestion).

We aim to acknowledge reports within **3 business days** and to agree on a
disclosure timeline with you (default: 90 days). Credit is given in the
advisory unless you prefer to stay anonymous.

## Scope

In scope: everything in this repository, including the default
`docker-compose` deployment.

High-value areas:
- **Tenant isolation** — PostgreSQL Row Level Security policies
  (`infra/postgres/migrations`), session GUCs set by the API, and the
  non-RLS fan-out paths (SSE live positions, webhooks, notifications).
- **Authentication** — JWT handling, API keys, one-time stream tickets.
- **Device ingestion** — JT808, JT1078, GT06 and RTMP listeners exposed to
  the internet (framing, buffer limits, timestamps, connection limits).
- **Remote commands** — engine stop/resume and device configuration.
- **Video access** — ZLMediaKit hooks (`on_play`, `on_publish`) and clip storage.

Out of scope: vulnerabilities in third-party dependencies without a
demonstrated impact on OpenMDVR (report those upstream), and issues that
require a compromised host or database superuser.

## Supported versions

The project is pre-1.0. Security fixes are applied to `main` only.

## Hardening guidance

See [docs/security-model.md](docs/security-model.md) for the threat model,
the controls already in place and the known limitations.
