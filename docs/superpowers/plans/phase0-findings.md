# Phase 0 Findings

Date run:
Fitbit Air paired since:

## Q1: Publishing status and refresh token lifetime

**ANSWERED — 2026-08-02.**

What happened when setting publishing status to "In production": published
straight through. No verification demand, no CASA security assessment prompt,
despite Google Health scopes being classified Restricted.

Does the personal-use exception apply: yes, in effect — the app reached
"In production" without a verification gate.

Refresh token lifetime: long-lived. The app is out of Testing status, so the
7-day test-user authorization expiry does not apply.

**Decision:** proceed as designed. The riskiest assumption in the spec holds;
no redesign needed. `auth.py`'s handling of revoked-token refresh failures
(Task 3) stays as designed — it is still the right behaviour if the grant is
ever revoked manually, it is simply no longer expected weekly.

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
