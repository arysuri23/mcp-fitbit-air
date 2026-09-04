# mcp-fitbit-air

An MCP server that lets Claude answer questions about your Google Fitbit Air
health data, through the Google Health API.

Ask things like *"How did I sleep this week?"*, *"Is my resting heart rate
trending up?"*, or *"Was my HRV low after Tuesday?"*

## Tools

| Tool | What it does |
|---|---|
| `get_daily_summary` | Day-by-day table: sleep, resting HR, HRV, steps, active zone minutes, SpO2, skin temperature. Start here. |
| `get_metric_series` | One metric over time, daily or intraday. |
| `get_sleep_detail` | Full sleep-stage breakdown for one night. |
| `get_profile_and_devices` | Profile, plus device battery and last sync time. |
| `query_raw` | Escape hatch for any data type the others do not cover. |

Every value carries its unit. Daily results from `get_daily_summary` and
`get_metric_series` also carry a trailing baseline drawn from up to 30 days
before the range you asked for, reported with the sample size it came from — a
baseline with a small `n` is not a settled norm.

Nothing is ever silently zero. Days the band recorded nothing come back as
`no_data`, metrics Fitbit has not computed yet as `warming_up`, and failures as
`error` with a remedy. A result that had to be cut short says so in
`truncated`.

## Requirements

- Python 3.11+
- A Fitbit device paired to a Google account
- A Google Cloud project with the Google Health API enabled

## Setup

### 1. Create a Google Cloud project

1. Create a project at [console.cloud.google.com](https://console.cloud.google.com/).
2. Enable the **Google Health API**.
3. Configure the OAuth consent screen (**External**) and add your own Google
   account as a **Test user**.
4. Create an **OAuth client ID** of type **Desktop app**. Note the client ID
   and secret.

> **Publishing status decides how often you re-authenticate.** Google Health
> scopes are *Restricted*. While the app sits in *Testing*, Google expires
> refresh tokens after **7 days**, so `mcp-fitbit-air auth` becomes a weekly
> chore and every tool will start returning an auth error until you run it.
> Moving to *In production* removes that; for personal single-user access there
> is an exception to the usual CASA security assessment.

### 2. Install

```bash
git clone <this repo>
cd mcp-fitbit-air
python3.11 -m venv .venv && source .venv/bin/activate   # or any 3.11+
pip install -e .
```

Name the interpreter version explicitly. The `python3` on a stock macOS is
3.9, and its bundled pip fails an editable install with a misleading complaint
about setuptools rather than saying the Python is too old.

### 3. Authenticate

```bash
cp .env.example .env
# edit .env and fill in your client ID and secret
mcp-fitbit-air auth
```

A browser opens for consent. The refresh token is written to
`~/.config/mcp-fitbit-air/token.json` with `0600` permissions, and is refreshed
silently until Google expires it — weekly under *Testing* status, see above.

`.env` is gitignored and so is the token. Exported environment variables take
precedence over `.env`, so the `env` block in your MCP client config always
wins.

### 4. Register with Claude

Add to your MCP client configuration (for Claude Desktop,
`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "fitbit-air": {
      "command": "/absolute/path/to/mcp-fitbit-air/.venv/bin/mcp-fitbit-air",
      "args": ["serve"],
      "env": {
        "FITBIT_MCP_CLIENT_ID": "your-client-id",
        "FITBIT_MCP_CLIENT_SECRET": "your-client-secret"
      }
    }
  }
}
```

Use the **absolute path** to the console script inside your virtualenv. MCP
clients do not run your shell profile, so a bare `mcp-fitbit-air` will usually
not be found. `python -m mcp_fitbit_air serve` works too, given the venv's own
`python`.

Restart the client, then ask: *"Is my Fitbit synced?"*

## Configuration

Set these as environment variables or in a `.env` file at the project root.
Environment variables win where both are present.

| Variable | Required | Default |
|---|---|---|
| `FITBIT_MCP_CLIENT_ID` | yes | — |
| `FITBIT_MCP_CLIENT_SECRET` | yes | — |
| `FITBIT_MCP_TOKEN_PATH` | no | `~/.config/mcp-fitbit-air/token.json` |

## Notes on the data

**Ranges end yesterday.** "Last week" means the seven days ending yesterday.
Today is always partial, and including it skews any comparison against it.
Ask for `"today"` explicitly if you want the partial day.

**Intraday is bucketed.** The band samples heart rate every few seconds — a
single day is tens of thousands of readings, far more than is useful or even
transmittable. `granularity="intraday"` returns time buckets with min, max,
average and count, sized automatically to keep the response readable.

**Intraday can be slow.** A large pull is followed by aggressive server-side
throttling, so an intraday request may sit for a minute or more before
returning. It is not stuck.

**Everything is live.** There is no local database; each call hits the API.

## Troubleshooting

**Every tool says "Run `mcp-fitbit-air auth`."** The refresh token expired or
was revoked. If this happens weekly, the app is still in *Testing* publishing
status — see step 1.

**Tools return `no_data`.** Call `get_profile_and_devices` and check
`lastSyncTime`. A stale sync explains missing data more often than anything
else does.

**HRV or skin temperature says `warming_up`.** These need several nights of
wear before Fitbit computes them. Expected on a new device.

**The client shows no tools.** Check the command path is absolute and points
inside the virtualenv. The server logs to stderr; your client's MCP log will
have the startup error.

## Development

```bash
pip install -e ".[dev]"
pytest                                   # offline, runs against fixtures
python scripts/smoke.py                  # manual, hits the live API
python scripts/smoke.py --skip-intraday  # ...without the slow one
```

The test suite never calls the live API. Fixtures in `tests/fixtures/` are
scrubbed real responses; regenerate them with `scripts/capture_fixtures.py` and
**review the output before committing** — they originate in real health data.

`scripts/smoke.py` exits non-zero if any tool returns an error, so it doubles
as a post-change check that the API still behaves as the fixtures claim.

## Design

See [`docs/superpowers/specs/2026-08-02-fitbit-air-mcp-design.md`](docs/superpowers/specs/2026-08-02-fitbit-air-mcp-design.md).

## Limitations

- Read-only. No logging of workouts, weight, or food.
- No local storage or caching.
- Single user.
- The legacy Fitbit Web API is deprecated on 2026-09-30; this server does not
  use it.
