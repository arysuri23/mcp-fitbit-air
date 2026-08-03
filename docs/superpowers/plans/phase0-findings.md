# Phase 0 Findings

Date run:
Fitbit Air paired since:

## Q1: Publishing status and refresh token lifetime

**ANSWERED — 2026-08-02. Revised the same day; see the correction below.**

First attempt: publishing to "In production" went straight through with no
verification demand. **That reading was wrong** — the app was not yet requesting
any restricted scope, so there was nothing to gate on.

Correction: once the `googlehealth.*` scopes were added, the app had to be
reverted to **Testing** publishing status. This is the documented behaviour:

- All Google Health scopes are classified **Restricted**.
- Restricted scopes cannot reach production without brand verification plus an
  annual CASA security assessment.
- The personal-use exception ("you are the only user of your app") exempts you
  from *verification*, not from the production gate. It keeps the app in an
  unverified state, subject to a user cap — which in practice means Testing.

**Refresh token lifetime: 7 days.** In Testing publishing status, test-user
authorizations expire 7 days from consent. The exemption for basic scopes
(name / email / profile only) does not apply here. Re-running
`mcp-fitbit-air auth` is expected roughly weekly.

**Decision: proceed as designed — no code changes needed.** The spec anticipated
this as the fallback case, and Task 3's `auth.py` already distinguishes an
expired/revoked refresh from "never authenticated" and names the Testing-status
7-day expiry in its remedy message. What was written as an edge case is simply
the normal weekly path now; the behaviour is identical.

**Open follow-up (does not block implementation):** retry "Publish app" now that
the scopes are attached. The 7-day rule keys off publishing status, not
verification status — so if Google permits production with an unverified-app
warning screen rather than hard-blocking, refresh tokens become long-lived.
If it hard-blocks, weekly re-auth stands.

Rejected alternatives: CASA assessment (cost and effort are aimed at real
products, not a personal tool); Workspace "Internal" user type (removes both the
verification requirement and the 7-day expiry, but requires a paid Google
Workspace domain — unavailable on a personal gmail.com account).

## Q2: API reachability

| Probe | HTTP status | Data present? | Notes |
|---|---|---|---|
| profile | | | |
| paired_devices | | | |
| list_sleep | | | |
| list_daily-resting-heart-rate | | | |
| list_daily-heart-rate-variability | | | |
| list_daily-oxygen-saturation | | | |
| list_daily-sleep-temperature-derivations | | | |
| rollup_steps_iso | | | |
| rollup_steps_civil | | | |
| rollup_active-zone-minutes_iso | | | |
| rollup_active-zone-minutes_civil | | | |

## Verified constants (later tasks depend on these)

- Working scope strings:
- `dailyRollUp` range shape that the API accepted (iso vs civil):
- Exact `filter` field prefix per data type (correct any that returned 400):
- Response envelope keys (`dataPoints` / `rollupDataPoints` / `nextPageToken`):
- Value field name inside each data point, per data type:
- Which metrics returned no data (warming up):

## Deviations from the spec

List anything the spec assumes that turned out to be wrong.
