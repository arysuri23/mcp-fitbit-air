# Fitbit Air MCP Server — Design

**Date:** 2026-08-02
**Status:** Approved, ready for implementation planning

## Purpose

An MCP server that lets Claude answer questions about the author's own health data
from a Google Fitbit Air, using the Google Health API.

Target questions:

- Reflection and Q&A on recent data — "How did I sleep this week?" "Is my resting
  heart rate trending up?" "Was my HRV low after Tuesday?"
- Longer-term trends and periodic reports — month-over-month baselines, "generate my
  monthly health summary."

Interpretation and insight generation are Claude's job, not the server's. The server's
responsibility is to return data that is *interpretable*: baselines alongside raw
values, units labeled, and gaps explicitly marked as gaps.

## Context

**Device.** Fitbit Air is screenless: optical PPG heart rate, red + IR pair (SpO2,
breathing rate), skin temperature, 3-axis accelerometer and gyroscope. It surfaces
steps, distance, calories, HRV, AFib detection, and sleep staging. Because there is no
screen, all data lives server-side in the Google Health app — so everything of interest
is API-reachable.

**API.** Build on the **Google Health API** (`health.googleapis.com`), not the legacy
Fitbit Web API. The legacy API is deprecated 2026-09-30; the two ran side by side from
May 2026. Google OAuth 2.0 replaces Fitbit's authorization. Endpoints follow a
consistent shape:

```
https://health.googleapis.com/v4/users/{userId}/dataTypes/{dataType}/dataPoints
```

with `list` (granular/intraday), `rollUp`, and `dailyRollup` methods. `userId` may be
`me`. Scopes take the form `https://www.googleapis.com/auth/googlehealth.{scope}` —
read-only scopes only for this project.

**Prior art.** [TheDigitalNinja/mcp-fitbit](https://github.com/TheDigitalNinja/mcp-fitbit)
is an existing TypeScript MCP server, but it targets the legacy Fitbit Web API, so both
its auth layer and all 15 of its tools stop working on 2026-09-30. Its tool surface
mirrors API endpoints rather than user questions, roughly a third of it covers manual
logging (food, nutrition, weight) that is irrelevant to an auto-tracking screenless
band, and it exposes none of the Air's distinctive metrics (HRV, SpO2, skin temperature,
breathing rate). Worth borrowing: its documented date-range limits and its OAuth setup
README as a model.

## Decisions

| Decision | Choice | Rationale |
|---|---|---|
| API | Google Health API v4 | Legacy Fitbit Web API dies 2026-09-30 |
| Audience | Single-user, built to open-source later | Fast to use daily; config-driven avoids a later refactor |
| Storage | None — live API calls | Account history is days old; a sync layer would serve no one |
| Language | Python | MCP Python SDK (`mcp` 2.x) + `google-auth` / `google-auth-oauthlib` |
| Transport | stdio | Launched as a subprocess by Claude Code / Desktop |
| Tool surface | Intent-shaped hybrid, 5 tools | Matches questions asked, not API structure |
| Alerts | Out of scope for v1 | MCP is passive; a scheduled agent is a separate later layer |
| Write access | Out of scope | Read-only scopes; the Air auto-tracks nearly everything |

### Explicitly out of scope for v1

- Local database / sync layer (see "Future-proofing" below)
- Scheduled alerts or morning-briefing agent
- Any write-back to Fitbit (workout, weight, or food logging)
- Importing history from other platforms (Apple Health, Oura, Whoop, Garmin)
- Multi-user support, hosted OAuth callback, CASA security assessment

## Architecture

Local Python MCP server over stdio. No hosting, no long-running callback server, no
daemon.

```
Claude ──stdio──▶ server.py        MCPServer; 5 tool definitions
                      │
                      ▼
                  mapping.py       friendly metric name → Google dataType,
                      │            rollup method, unit, expected range
                      ▼
                  client.py        HTTP against health.googleapis.com
                      │            ← seam where caching slots in later
                      ▼
                   auth.py         credential load + silent token refresh
                      │
                      ▼
          ~/.config/mcp-fitbit-air/token.json   (mode 0600)
```

Each module has one responsibility and can be tested independently: `mapping.py` is
pure data plus pure functions; `client.py` does HTTP and knows nothing about MCP;
`auth.py` does credentials and nothing else; `server.py` defines tools and delegates.

### Authentication

OAuth for an installed app requires a browser and a loopback redirect. An MCP server
speaking stdio cannot run an interactive browser login mid-request: it would hang the
tool call, and anything written to stdout would corrupt the protocol stream.

Therefore authentication is a **separate one-time CLI command**:

- `mcp-fitbit-air auth` — opens a browser, completes consent on a loopback redirect,
  writes the refresh token to disk at mode 0600.
- The **server only ever refreshes silently.** If it starts without valid credentials,
  it does not attempt to fix this; every tool returns an `error` result instructing the
  user to run `mcp-fitbit-air auth`.

### Configuration

Entirely from environment variables — nothing hardcoded, which is what makes
open-sourcing a README rather than a refactor:

- `FITBIT_MCP_CLIENT_ID` (required)
- `FITBIT_MCP_CLIENT_SECRET` (required)
- `FITBIT_MCP_TOKEN_PATH` (optional; defaults to `~/.config/mcp-fitbit-air/token.json`)

### Future-proofing

`client.py` is the single seam through which all API access flows. Adding a local cache
or sync layer later means changing that module only — no tool signature changes. This
is the one piece of forward-looking design retained, because it is nearly free.

## Phase 0 — De-risking spike

A throwaway script, run before anything else is built. It answers the questions that
could invalidate the whole design:

1. **Does the personal-use exception permit publishing to production with restricted
   scopes?** All Google Health API scopes are classified *Restricted*, which normally
   requires an annual CASA security assessment. A documented personal-use exception
   exists and is expected to apply. This must be confirmed, because the fallback —
   leaving the app in "Testing" publishing status — expires refresh tokens every 7
   days, forcing weekly re-authentication.
2. **Does Fitbit Air data actually surface through the v4 API,** and in what shape?

The spike targets `users.getProfile` and `users.pairedDevices` **first**: these return
real JSON the moment auth works, including the Air's battery level and last sync time.
That isolates "is auth working" from "is there data." It then pulls a few days of sleep
and heart rate data to capture real response shapes.

No further implementation begins until the spike returns real JSON. Its responses become
the first test fixtures.

## Tools

### `get_daily_summary(start_date, end_date)`

The workhorse; should answer most questions in a single call. Returns one row per day:

- Sleep duration and stage breakdown
- Resting heart rate
- HRV
- Steps
- Active zone minutes
- SpO2
- Skin temperature deviation

Fans out **concurrently** and stitches results into a per-day table. The fan-out is
*mixed*, because data types support different methods: `steps` and `active-zone-minutes`
use `dailyRollUp`, while `sleep` and the pre-aggregated `daily-*` types
(`daily-resting-heart-rate`, `daily-heart-rate-variability`, `daily-oxygen-saturation`,
`daily-sleep-temperature-derivations`) support only `list`, filtered by an AIP-160
`filter` expression. Per-metric method selection lives in the mapping table.

Capped at **90 days** per call, matching the API's own limit for these types; beyond
that, returns an `error` naming the cap and suggesting a narrower range.

### `get_metric_series(metric, start_date, end_date, granularity)`

One metric over time. `granularity` is `daily` or `intraday`.

Valid `metric` values are the friendly names defined in `mapping.py` — the same
vocabulary used as column names by `get_daily_summary` (`sleep_duration`,
`resting_heart_rate`, `hrv`, `steps`, `active_zone_minutes`, `spo2`,
`skin_temperature_deviation`). The mapping table is the single source of truth; the tool
description enumerates the names, and an unrecognized value returns an `error` listing
the valid options rather than failing opaquely.

Intraday is force-capped to **7 days** regardless of the range requested, because
minute-level data over longer spans would overwhelm the context window. (The API's own
limit for `heart-rate` is 14 days, so this cap is the stricter of the two.) When
truncation occurs the response states plainly that it happened and why.

### `get_sleep_detail(date)`

One night, full stage segments with timings. Separate from the daily summary because the
response shape differs fundamentally — a variable-length segment list rather than a
single row — and it is needed only occasionally.

### `get_profile_and_devices()`

Age, height, weight, timezone, plus the Air's battery level and last sync time. Doubles
as the end-to-end smoke test: "Is my Air synced?" is both genuinely useful and a full
stack verification.

### `query_raw(data_type, start_date, end_date, method)`

Escape hatch for anything unmapped. The valid `data_type` vocabulary and `method` values
are documented in the tool description itself, so no separate discovery tool is needed.

### Cross-cutting tool behavior

**Natural-language dates.** All date parameters accept `"last week"`, `"yesterday"`,
`"2026-07-28"`, and similar, resolved in the user's profile timezone. Without this,
Claude must perform date arithmetic before every call — a reliable source of off-by-one
errors, particularly around "this week."

**Baselines.** `get_daily_summary` and `get_metric_series` include each metric's
trailing 30-day mean alongside the raw value. (`get_sleep_detail`, `get_profile_and_devices`,
and `query_raw` do not — segment timings, profile fields, and raw passthrough have no
meaningful baseline.) This is the highest-leverage element for answer quality:
"HRV 42ms" is noise; "HRV 42ms against a 58ms baseline" is a finding. It costs one extra
rollup call and is what allows insight to emerge without a bespoke analysis tool. While
account history is short, the baseline is computed over whatever data exists and
**labeled with its sample size**, so Claude does not over-read a 4-day mean.

**Units and ranges labeled.** Every value carries its unit (milliseconds vs. seconds,
°C deviation vs. absolute). Ambiguity here produces confidently wrong interpretations.

## Response contract

Every tool returns a structured result that always distinguishes four states. Empty
results are never collapsed into a bare empty list.

| State | Meaning | Expected Claude behavior |
|---|---|---|
| `ok` | Data present | Answer the question |
| `no_data` | Query succeeded, nothing recorded — band not worn, or day incomplete | Say so; suggest checking sync |
| `warming_up` | Metric needs more nights before Fitbit computes it; includes nights-so-far | Explain the wait; do not invent a number |
| `error` | Auth, rate limit, or API failure — names which, and the fix | Surface the actual problem |

`no_data` and `warming_up` are **not errors**. They are legitimate answers to "how did I
sleep?" on a night the band was not worn. This distinction is a core requirement rather
than an error-handling detail: with an account only days old, "empty" is a common case,
and without explicit states it is indistinguishable from "broken." Several metrics —
HRV and skin temperature especially — require multiple nights of wear before Fitbit
computes baselines at all.

**Partial results are expected and valid.** If a 7-day summary has HRV for four days, it
returns four days of HRV and marks the remainder — it does not fail the call.

## Error handling

- **Missing or expired credentials** → every tool returns the same actionable message:
  run `mcp-fitbit-air auth`. Never a stack trace.
- **Revoked refresh token** → distinguished from "never authenticated," because the fix
  differs. Expected if the app remains in Testing publishing status (7-day expiry).
- **Rate limiting (429)** → bounded retry with exponential backoff, then an honest
  "rate limited, try again in N minutes."
- **Partial fan-out failure** in `get_daily_summary` → one metric failing does not sink
  the other six; that metric is marked `error` in its column while the rest return
  normally.

## Testing

Three layers. **No live API calls in the test suite.**

1. **Recorded fixtures.** Real JSON captured from the Phase 0 spike and first real
   queries, scrubbed and committed. All tests run against these, making the suite
   deterministic and grounded in actual API shapes rather than assumptions about them.
   The capture script strips user IDs and offsets dates before anything is committed —
   fixtures are real health data.
2. **Unit tests** on pure logic: date parsing (including month boundaries and DST), the
   dataType mapping table, baseline math with small sample sizes, unit conversions.
3. **Contract tests** asserting every tool returns a valid state for each of the four
   cases — including `no_data` and `warming_up`, which are hard to reproduce live but
   trivial with fixtures.

Additionally, a **manual smoke script**, run by hand against the live API, calls each
tool once and prints results. It verifies that reality still matches the fixtures —
particularly relevant given the Google Health API is only months old and may still
change.

## Success criteria

- `mcp-fitbit-air auth` completes once and the server refreshes silently thereafter,
  with no weekly re-authentication.
- "How did I sleep this week?" is answered correctly in a single `get_daily_summary`
  call.
- Days with no wear are reported as `no_data`, and not-yet-computed metrics as
  `warming_up` — neither is ever presented as zero or as an error.
- Test suite passes offline against fixtures.
- A second person could set it up from the README with their own Google Cloud project.

## References

- [Google Health API — migration overview](https://developers.google.com/health/migration)
- [Google Health API — API specifications](https://developers.google.com/health/migration/api-specifications)
- [Google Health API — about](https://developers.google.com/health/about)
- [Google Health API — app verification](https://developers.google.com/health/app-verification)
- [Restricted scope verification](https://developers.google.com/identity/protocols/oauth2/production-readiness/restricted-scope-verification)
- [TheDigitalNinja/mcp-fitbit](https://github.com/TheDigitalNinja/mcp-fitbit) — prior art, legacy API
