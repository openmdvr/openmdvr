# Security issues in common MDVR platforms, and how OpenMDVR addresses them

Most commercial MDVR and dashcam fleets are managed through a small number of
closed-source platforms supplied with the hardware. Several of them have
publicly documented, critical vulnerabilities. This page lists what is on the
public record, the patterns behind those issues, and how OpenMDVR is designed
against each pattern.

**Scope and fairness.** Only publicly documented issues are listed, each with
its source. Version numbers are the ones named in each record; later versions
may be fixed, and vendors may have released patches not reflected here. The
list is not exhaustive and is not a claim that any particular deployment is
vulnerable today. Product names are trademarks of their owners. Corrections
are welcome: open an issue with a source.

## Publicly documented issues

| Platform | Record | Class | Severity (CVSS 3.1) | Notes |
|---|---|---|---|---|
| CMSV6 (vendor in NVD: Tongtianxing Technology Co., Ltd.) | [CVE-2024-29666](https://nvd.nist.gov/vuln/detail/CVE-2024-29666) | Use of default password (CWE-1393) | 9.8 Critical | Remote privilege escalation through a default password. |
| CMSV6 v7.31.0.2 to v7.31.0.3 | [CVE-2024-29667](https://nvd.nist.gov/vuln/detail/CVE-2024-29667) | SQL injection | 9.8 Critical | Remote, unauthenticated; privilege escalation and data disclosure via the `ids` parameter. |
| Streamax Crocus 1.3.40 | [CVE-2025-11909](https://nvd.nist.gov/vuln/detail/CVE-2025-11909), [CVE-2025-11910](https://nvd.nist.gov/vuln/detail/CVE-2025-11910), [CVE-2025-11911](https://nvd.nist.gov/vuln/detail/CVE-2025-11911), [CVE-2025-11912](https://nvd.nist.gov/vuln/detail/CVE-2025-11912) | SQL injection through sort parameters (`orderField`, `sortField`) | 6.3 Medium (per record) | Four endpoints. The records state the vendor did not respond to early disclosure. |
| Streamax Crocus 1.3.40 | [CVE-2025-11908](https://nvd.nist.gov/vuln/detail/CVE-2025-11908) | Unrestricted file upload | 6.3 Medium | File upload endpoint accepts arbitrary files. |
| Streamax Crocus 1.3.40 | [CVE-2025-11913](https://nvd.nist.gov/vuln/detail/CVE-2025-11913), [CVE-2025-11914](https://nvd.nist.gov/vuln/detail/CVE-2025-11914) | Path traversal | 4.3 Medium | File download endpoints take a client-supplied path. |
| Streamax Crocus 1.3.44 | [CVE-2026-52470](https://nvd.nist.gov/vuln/detail/CVE-2026-52470) | SQL injection | 9.8 Critical | Remote privilege escalation; a public exploit write-up is referenced. |
| Streamax CEIBA II client | Vendor user manual (v2.3, p. 12) | Default credentials | Not a CVE | The official manual states that "the default username and password are both 'admin'". |

## Recurring patterns

Read together, these records show a handful of patterns rather than isolated
bugs:

1. **SQL injection**, often through parameters that change the query shape
   (sort fields, ID lists) and that are concatenated into SQL.
2. **Default or shared credentials** shipped by the vendor and documented in
   manuals.
3. **File handling that trusts the client**: arbitrary uploads and download
   paths taken from the request.
4. **No defense in depth**: once one query or one account is compromised,
   nothing in the data layer limits what the attacker can reach.
5. **Closed source and unanswered disclosure**: operators cannot audit the
   code, and some reports received no vendor response.

## How OpenMDVR addresses each pattern

### 1. SQL injection

- Every query is parameterized: `psycopg` placeholders in the API, `pgx`
  arguments in the device server. User input is never concatenated into SQL.
- No endpoint accepts a sort field or column name from the client. Ordering
  is fixed in the query, which removes the exact vector behind the
  `orderField`/`sortField` injections above. Dynamic `WHERE` clauses are
  built only from constant fragments with bound parameters.
- Even a hypothetical injection would run as `app_user`, a non-superuser role
  under **forced PostgreSQL Row Level Security**: it could not read or modify
  another tenant's rows (see pattern 4).

### 2. Default credentials

- There is no default account. The first administrator is created with
  `api/scripts/bootstrap_admin.py`, which prompts for a password (minimum
  8 characters) and keeps it out of the shell history.
- `docker compose` refuses to start without the database, JWT, API-key and
  media-server secrets, and `infra/init-env.sh` generates each one randomly.
  Two installations never share a secret.
- Devices are not trusted by default either: a tracker or camera is accepted
  only if its identifier was provisioned by an administrator.
- Login returns one generic error for unknown accounts and wrong passwords,
  and spends the same bcrypt time in both cases, so accounts cannot be
  enumerated.

### 3. File handling

- Clients never choose a storage path. Video clip keys are built by the
  server (`tenants/<tenant id>/alarm-clips/<random id>.ts`), and a database
  `CHECK` constraint rejects any key outside the tenant's own prefix.
- The upload endpoint used by cameras accepts a file only while that device
  holds an authenticated protocol session, caps the size, and correlates the
  file to a pending request for that same device. A file whose embedded
  timestamp does not match its alarm is quarantined, never attached.
- Downloads are signed object-storage URLs that expire after one hour, not a
  server endpoint that reads a path from the request. Snapshot photos are
  never written to storage at all; they live briefly in an in-memory cache.

### 4. Defense in depth

- **Tenant isolation lives in PostgreSQL.** Every business table has a
  tenant id and a forced RLS policy; time-series tables are reachable only
  through security-barrier views and audited write functions. A bug in the
  API returns nothing from another tenant instead of leaking it.
- **Video access requires one-time tickets** bound to tenant, device,
  channel and stream namespace, checked by the media server before the
  first byte. A stream URL alone is useless.
- **Sessions are revalidated on every request**: disabling a user or
  suspending a tenant cuts access immediately, including open streams.
- **Device ingestion is hardened** against hostile traffic: bounded buffers,
  panic recovery per connection, connection caps, and bounds on
  device-reported timestamps.

### 5. Openness and disclosure

- All code is open under Apache-2.0, so operators and researchers can audit
  exactly what runs.
- Vulnerabilities are reported privately through GitHub security advisories
  ([SECURITY.md](../SECURITY.md)), with a documented response process.
- Features that touch authentication, isolation or device ingestion have
  gone through adversarial reviews that attacked the running system; the
  findings and fixes are summarized in [security-model.md](security-model.md).
- Dependencies are pinned, CI runs `govulncheck` and a secret scanner, and
  Dependabot tracks updates.

## What OpenMDVR cannot fix

Honesty about limits matters as much as the list above.

- **Device firmware.** OpenMDVR does not change the software running on the
  cameras and trackers. Vulnerabilities in device firmware, and default
  passwords on the devices themselves, must be handled with the
  manufacturer.
- **Device protocols are plaintext.** JT/T 808, JT/T 1078 and GT06 have no
  transport encryption in their standards, and GT06 has no cryptographic
  device authentication. OpenMDVR validates and bounds everything it
  receives, but traffic between a device and the server can be observed on
  the network path. Where that matters, use a private APN or a VPN between
  the cellular network and the server.
- **Not certified.** OpenMDVR is not certified under C-TPAT, FMCSA ELD or any
  other program. It is designed to support security programs, not to
  replace an assessment.

## Sources

- NVD and GitHub Advisory Database records linked in the table above.
- CMSV6 original reports: [default password](https://github.com/whgojp/cve-reports/wiki/There-is-a-weak-password-in-the-CMSV6-vehicle-monitoring-platform-system), [SQL injection](https://github.com/whgojp/cve-reports/wiki/CMSV6-vehicle-monitoring-platform-system-SQL-injection).
- Streamax Crocus records: [OpenCVE vendor listing](https://app.opencve.io/cve/?vendor=streamax).
- CEIBA II: *User Manual For CEIBA II Client (V2.3)*, Streamax, section on client login (p. 12).

*Last reviewed: October 2026.*
