# Phase 0 Findings

Date run: 2026-08-02 (round 1), 2026-08-02/03 (rounds 2–4)
Fitbit Air paired since: 2026-07-28
Device confirmed: `deviceVersion: "Fitbit Air"`, `deviceType: TRACKER`, battery 97%

**Verdict: proceed. The architecture is unchanged. The data-access details in the
plan were substantially wrong and are corrected below.**

---

## Q1: Publishing status and refresh token lifetime

**ANSWERED — with a correction.**

First attempt: publishing to "In production" went straight through. **That reading
was wrong** — no restricted scope was attached yet, so nothing gated.

Once the `googlehealth.*` scopes were added, the app had to revert to **Testing**:

- All Google Health scopes are classified **Restricted**.
- Restricted scopes cannot reach production without brand verification plus an
  annual CASA security assessment.
- The personal-use exception ("you are the only user of your app") exempts you
  from *verification*, not from the production gate. It keeps the app unverified,
  which in practice means Testing.

**Refresh token lifetime: 7 days.** Test-user authorizations expire 7 days from
consent. Re-running `mcp-fitbit-air auth` is expected roughly weekly. No code
change needed: Task 3's `auth.py` already distinguishes an expired/revoked
refresh from "never authenticated" and names this cause in its remedy.

Open follow-up (non-blocking): retry "Publish app" now the scopes are attached.
The 7-day rule keys off publishing status, not verification status.

Rejected: CASA assessment (aimed at real products); Workspace "Internal" user
type (needs a paid Workspace domain, unavailable on a personal gmail.com).

---

## Q2: API reachability

All probes below returned HTTP 200 in the final round.

| Probe | Result |
|---|---|
| `users/me/profile` | 200 — needs `profile.readonly` |
| `users/me/settings` | 200 — needs `settings.readonly` |
| `users/me/pairedDevices` | 200 — 1 device, the Air |
| `sleep` (list) | 200 — sessions with 24 stage segments |
| `daily-resting-heart-rate` | 200 |
| `daily-heart-rate-variability` | 200 |
| `daily-oxygen-saturation` | 200 |
| `daily-sleep-temperature-derivations` | 200 |
| `steps` (dailyRollUp) | 200 — 7 daily buckets |
| `active-zone-minutes` (dailyRollUp) | 200 — 5 daily buckets |
| `heart-rate` (list) | 200 — per-sample readings |

**No metric is warming up.** Every metric the spec asks for already has data,
including HRV and skin temperature. `warming_up` remains a required state (a
non-worn night still yields nothing) but is not the current condition.

---

## Verified constants

### Scopes (all five required)

```
https://www.googleapis.com/auth/googlehealth.activity_and_fitness.readonly
https://www.googleapis.com/auth/googlehealth.health_metrics_and_measurements.readonly
https://www.googleapis.com/auth/googlehealth.sleep.readonly
https://www.googleapis.com/auth/googlehealth.profile.readonly
https://www.googleapis.com/auth/googlehealth.settings.readonly
```

The last two were missing in round 1 and caused 403
`ACCESS_TOKEN_SCOPE_INSUFFICIENT` on profile and pairedDevices.

**Consent gotcha.** Adding scopes to the code is not enough — they must also be
added to the OAuth consent screen's Data Access configuration. Until then Google
grants only the previously-configured subset, and `oauthlib` aborts the flow with
`Warning: Scope has changed from ... to ...`. Task 3 must tolerate a granted-scope
set that differs from the requested one by setting `OAUTHLIB_RELAX_TOKEN_SCOPE=1`
before the flow runs, and should surface a clear message naming the Data Access
page when a required scope is absent.

### dailyRollUp request shape

`range.start` / `range.end` are **nested CivilDateTime objects**. Neither ISO
`startTime`/`endTime` strings nor bare `{year,month,day}` are accepted.

```json
POST /v4/users/me/dataTypes/steps/dataPoints:dailyRollUp
{
  "range": {
    "start": {"date": {"year": 2026, "month": 7, "day": 26}},
    "end":   {"date": {"year": 2026, "month": 8,  "day": 2}}
  },
  "windowSizeDays": 1
}
```

An optional `"time": {...}` member is also accepted but unnecessary. Response:
`{"rollupDataPoints": [{"civilStartTime": {...}, "civilEndTime": {...}, "<type>": {...}}]}`.

### Filter dialects — three of them, and the wrong one fails *silently*

This is the most dangerous finding. A filter naming a valid-but-unmatched member
returns **HTTP 200 with zero data points**, indistinguishable from "no data".

| Data types | Filter member | Literal | Verified |
|---|---|---|---|
| `daily-*` | `<snake_type>.date` | `"2026-08-01"` | 4 points |
| `sleep` | `sleep.interval.civil_end_time` | `"2026-08-01"` | 2–4 points |
| `steps`, `active-zone-minutes` | `steps.interval.start_time` | `"2026-08-01T00:00:00Z"` | 1 point |
| `heart-rate` | `heart_rate.sample_time.physical_time` | `"2026-08-01T00:00:00Z"` | 3 points |

Civil-time filters on `steps` and `heart-rate` return 0 points with **both** date
and datetime literals — they are accepted and silently match nothing. Physical
time (RFC-3339, `Z`) is the only reliable form for those types. The `daily-*`
`.date` member and `sleep`'s `civil_end_time` are the exceptions that do work.

Consequence: physical-time filters need local dates converted to UTC instants,
so the server must know the user's timezone (see below).

### Timezone lives in settings, not profile

`users/me/profile` has **no timezone field** — only `name`, `age`,
`membershipStartDate`, and stride lengths. The plan's
`profile.get("timezone")` would always have been `None`.

`users/me/settings` carries `timeZone` (IANA name) and `utcOffset`. Data points
additionally carry per-record offsets (`startUtcOffset`, `utcOffset`, e.g.
`"-14400s"`).

### Value paths — none matched the plan's guesses

The plan probed for keys named `value`, `count`, `bpm`, `durationSeconds`. The
real paths:

| Metric | Path | Type | Unit |
|---|---|---|---|
| `sleep_duration` | `sleep.summary.minutesAsleep` | **str** | **minutes** |
| `resting_heart_rate` | `dailyRestingHeartRate.beatsPerMinute` | **str** | bpm |
| `hrv` | `dailyHeartRateVariability.averageHeartRateVariabilityMilliseconds` | float | ms |
| `spo2` | `dailyOxygenSaturation.averagePercentage` | float | percent |
| `skin_temperature_deviation` | `dailySleepTemperatureDerivations.nightlyTemperatureCelsius` − `.baselineTemperatureCelsius` | float | °C deviation |
| `steps` (rollup) | `steps.countSum` | **str** | count |
| `active_zone_minutes` (rollup) | `activeZoneMinutes.sumInFatBurnHeartZone` / `sumInCardioHeartZone` / `sumInPeakHeartZone` | **str** | minutes |
| `heart_rate` (list) | `heartRate.beatsPerMinute` | **str** | bpm |

**Integers arrive as JSON strings** throughout (`"8630"`, `"61"`, `"385"`).
Floats arrive as real numbers. Extraction must coerce.

**Sleep duration is in minutes, not seconds** — the spec's stated unit was wrong.

**Skin temperature deviation is derived**, not returned: nightly minus baseline.
`relativeNightlyStddev30dCelsius` is also available.

**Active Zone Minutes is split by zone.** Fitbit's headline AZM counts cardio and
peak double: `fat_burn + 2 × (cardio + peak)`. Report that total and include the
per-zone breakdown so the number matches the app and stays inspectable.

### Date extraction — three shapes

| Type family | Date location |
|---|---|
| `daily-*` | `<payload>.date` → `{year, month, day}` |
| `sleep`, `steps`, `active-zone-minutes` | `<payload>.interval.civilStartTime.date` / `civilEndTime.date` |
| `heart-rate` | `heartRate.sampleTime.civilTime.date` |
| rollup responses | `civilStartTime.date` at the **top level** of the rollup point |

### Sleep detail

`sleep.stages` is a list of `{startTime, endTime, startUtcOffset, endUtcOffset,
type}` with `type` ∈ `AWAKE` / `LIGHT` / `DEEP` / `REM`.
`sleep.summary.stagesSummary` gives per-stage `{type, minutes, count}`.
`sleep.metadata.mainSleep` distinguishes the main sleep from naps.
`sleep.type` is `STAGES`.

### Device fields

`pairedDevices[].{deviceVersion, deviceType, batteryLevel, batteryStatus,
lastSyncTime, macAddress, features}`. `features` includes `SLEEP`,
`NIGHTTIME_OXYGEN_SATURATION`, `ACTIVE_MINUTES`, `GPS`.
`macAddress` is an identifier — the fixture scrubber must redact it.

---

## Deviations from the spec

1. **Sleep duration unit** — minutes, not seconds.
2. **Timezone source** — `settings.timeZone`, not a profile field that does not exist.
3. **Filter syntax is per-type, in three dialects**, and the wrong one fails silently.
4. **`dailyRollUp` range** is a nested CivilDateTime, not ISO strings.
5. **Numeric values arrive as strings** and must be coerced.
6. **Skin temperature deviation is derived**, not a returned field.
7. **AZM is per-zone**, requiring Fitbit's weighted formula for a headline number.
8. **Two extra scopes** (`profile.readonly`, `settings.readonly`) are required.
9. **No metric is warming up** — all seven have data at 5 days of wear.
