# 0005 — Header-based RBAC for the demo

**Status:** accepted (demo only)

## Context
The UI needs to show role-dependent behaviour (read-only viewers, PII masking) without an identity provider.

## Decision
Roles come from the `X-Atlas-Role` header: `viewer` (read-only, account ids and names masked), `engineer`
(can inject faults and remediate, names masked), `admin` (can reset, PII visible). Classification comes from the
data contract (`classification: pii | confidential | internal`).

## Production
Replace the header with OIDC (e.g. Entra ID / Cognito) and map groups to roles; enforce masking in the warehouse
(Unity Catalog column masks / Lake Formation) rather than in the API, and audit every PII read.
