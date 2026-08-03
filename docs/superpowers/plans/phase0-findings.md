# Phase 0 Findings

Date run:
Fitbit Air paired since:

## Q1: Publishing status and refresh token lifetime

What happened when setting publishing status to "In production":
Does the personal-use exception apply:
Refresh token lifetime (7 days if stuck in Testing, else long-lived):
**Decision:** proceed as designed / redesign needed because:

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
