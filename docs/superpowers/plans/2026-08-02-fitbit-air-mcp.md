# Fitbit Air MCP Server Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A local Python MCP server exposing five intent-shaped tools that let Claude answer questions about the author's Fitbit Air health data via the Google Health API.

**Architecture:** A stdio MCP server built on the official MCP Python SDK. Tool functions in `server.py` delegate to a layered core: `mapping.py` (metric registry — the single source of truth for data types, methods, units, and limits), `client.py` (all HTTP against `health.googleapis.com`, the seam where caching would later slot in), and `auth.py` (credential loading and silent refresh). Interactive OAuth lives in a separate CLI command, never in the server, because a stdio server cannot open a browser mid-request without corrupting the protocol stream.

**Tech Stack:** Python 3.11+, `mcp` 2.x, `google-auth`, `google-auth-oauthlib`, `requests` (via `google.auth.transport.requests.AuthorizedSession`), `pytest`, `responses` (HTTP mocking).

## Global Constraints

- **Python 3.11+.** Verified environment: Python 3.13.14 at `.venv/`. The `mcp` package itself requires ≥3.10, so macOS system Python 3.9 cannot run this project.
- **MCP SDK is `mcp` 2.x**, where the server class is `MCPServer`, imported as `from mcp.server.mcpserver import MCPServer`. The `FastMCP` name and the `mcp.server.fastmcp` module belong to the 1.x line and do not exist in 2.x. Everything else is unchanged: `@server.tool()` derives the schema from type hints and the description from the docstring, `run()` defaults to stdio transport, and returning a `dict` is serialised for you.
- **Read-only scopes only.** No write scope may be requested anywhere in this project.
- **No live API calls in the test suite.** Every test runs against committed fixtures.
- **Fixtures are real health data** — the capture script must strip user IDs and offset dates before anything is committed.
- **Token file is written at mode `0600`**, never committed, and `.gitignore`d.
- **Config comes only from environment variables.** No client ID, secret, or project ID may be hardcoded in any source file.
- **API base:** `https://health.googleapis.com/v4`
- **Query range limits (API-imposed):** 14 days for `heart-rate`, `active-minutes`, `total-calories`, `calories-in-heart-rate-zone`; 90 days for all other data types.
- **Never emit to stdout** anywhere in the server process — stdout is the MCP protocol stream. All diagnostics go to stderr via `logging`.

---

### Task 1: Phase 0 de-risking spike

This task gates every other task. Do not start Task 2 until this one's findings document is written and both questions below are answered.

The spike answers two questions that could invalidate the design:

1. **Does the personal-use exception permit publishing to production with restricted scopes?** All Google Health scopes are *Restricted*, which normally requires an annual CASA assessment. If the exception does not apply and the app must stay in "Testing" publishing status, refresh tokens expire every 7 days and the design needs rework.
2. **Does Fitbit Air data actually surface through the v4 API, and in what JSON shape?**

**Files:**
- Create: `spike/phase0_spike.py`
- Create: `spike/README.md`
- Create: `docs/superpowers/plans/phase0-findings.md`
- Create: `.gitignore`

**Interfaces:**
- Consumes: nothing (first task)
- Produces: `docs/superpowers/plans/phase0-findings.md` containing verified values that later tasks depend on — the exact `range` object shape for `dailyRollUp`, the exact `filter` field prefixes for `list`, the working scope strings, and the response envelope keys. Raw JSON responses saved to `spike/raw/` for conversion into fixtures in Task 6.

- [ ] **Step 1: Create `.gitignore`**

```
__pycache__/
*.pyc
.venv/
venv/
.pytest_cache/
*.egg-info/
dist/
build/

# Never commit credentials or raw health data
token.json
client_secret*.json
spike/raw/
.env
```

- [ ] **Step 2: Set up the Google Cloud project manually**

This is a human step, not code. Follow it exactly:

1. Create a Google Cloud project at https://console.cloud.google.com/
2. Enable the **Google Health API** for the project.
3. Configure the OAuth consent screen. Select **External** user type.
4. Add your own Google account (the one paired to the Fitbit Air) as a **Test user**.
5. Create an **OAuth client ID** of type **Desktop app**. Download the client ID and secret.
6. **Attempt to set publishing status to "In production."** Record exactly what happens — whether it publishes immediately, demands a CASA assessment, or offers a personal-use path. *This is the answer to question 1.* Record it verbatim in the findings doc.
7. Export the credentials into your shell:

```bash
export FITBIT_MCP_CLIENT_ID='<your client id>'
export FITBIT_MCP_CLIENT_SECRET='<your client secret>'
```

- [ ] **Step 3: Create the virtual environment and install dependencies**

```bash
cd /Users/arysuri/side_projects/mcp-fitbit-air
python3 -m venv .venv
source .venv/bin/activate
pip install google-auth google-auth-oauthlib requests
```

- [ ] **Step 4: Write the spike script**

Create `spike/phase0_spike.py`:

```python
"""Phase 0 de-risking spike. Throwaway — not production code.

Verifies that OAuth works and that Fitbit Air data is reachable through the
Google Health API v4. Saves every raw response to spike/raw/ for later
conversion into test fixtures.
"""

import json
import os
import sys
from datetime import date, timedelta
from pathlib import Path

from google.auth.transport.requests import AuthorizedSession
from google_auth_oauthlib.flow import InstalledAppFlow

BASE = "https://health.googleapis.com/v4"
RAW = Path(__file__).parent / "raw"

# Candidate scopes. If any is rejected, record the error verbatim in findings.
SCOPES = [
    "https://www.googleapis.com/auth/googlehealth.activity_and_fitness.readonly",
    "https://www.googleapis.com/auth/googlehealth.health_metrics_and_measurements.readonly",
    "https://www.googleapis.com/auth/googlehealth.sleep.readonly",
]


def authenticate() -> AuthorizedSession:
    client_id = os.environ["FITBIT_MCP_CLIENT_ID"]
    client_secret = os.environ["FITBIT_MCP_CLIENT_SECRET"]
    client_config = {
        "installed": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost"],
        }
    }
    flow = InstalledAppFlow.from_client_config(client_config, scopes=SCOPES)
    creds = flow.run_local_server(port=0, access_type="offline", prompt="consent")
    print(f"refresh_token present: {creds.refresh_token is not None}", file=sys.stderr)
    (RAW / "token_shape.json").write_text(
        json.dumps(
            {
                "has_refresh_token": creds.refresh_token is not None,
                "scopes": creds.scopes,
                "expiry": str(creds.expiry),
            },
            indent=2,
        )
    )
    return AuthorizedSession(creds)


def save(name: str, status: int, body) -> None:
    RAW.mkdir(exist_ok=True)
    (RAW / f"{name}.json").write_text(
        json.dumps({"status": status, "body": body}, indent=2, default=str)
    )
    print(f"  -> saved {name}.json (HTTP {status})", file=sys.stderr)


def probe_get(session: AuthorizedSession, name: str, url: str, **params) -> None:
    print(f"GET {name}: {url} {params}", file=sys.stderr)
    try:
        resp = session.get(url, params=params, timeout=30)
        body = resp.json() if resp.content else None
        save(name, resp.status_code, body)
    except Exception as exc:  # spike: record everything, fail nothing
        save(name, -1, {"exception": repr(exc)})


def probe_post(session: AuthorizedSession, name: str, url: str, payload: dict) -> None:
    print(f"POST {name}: {url}", file=sys.stderr)
    try:
        resp = session.post(url, json=payload, timeout=30)
        body = resp.json() if resp.content else None
        save(name, resp.status_code, {"request": payload, "response": body})
    except Exception as exc:
        save(name, -1, {"request": payload, "exception": repr(exc)})


def main() -> None:
    RAW.mkdir(exist_ok=True)
    session = authenticate()

    # --- Question A: does auth work at all? These return data immediately. ---
    probe_get(session, "profile", f"{BASE}/users/me/profile")
    probe_get(session, "paired_devices", f"{BASE}/users/me/pairedDevices")

    end = date.today()
    start = end - timedelta(days=7)

    # --- Question B: what shape is the data? ---
    # list-only types, filtered via AIP-160 filter expressions.
    for data_type, field in [
        ("sleep", "sleep.interval.civil_end_time"),
        ("daily-resting-heart-rate", "daily_resting_heart_rate.interval.civil_start_time"),
        ("daily-heart-rate-variability", "daily_heart_rate_variability.interval.civil_start_time"),
        ("daily-oxygen-saturation", "daily_oxygen_saturation.interval.civil_start_time"),
        ("daily-sleep-temperature-derivations",
         "daily_sleep_temperature_derivations.interval.civil_start_time"),
    ]:
        probe_get(
            session,
            f"list_{data_type}",
            f"{BASE}/users/me/dataTypes/{data_type}/dataPoints",
            filter=f'{field} >= "{start.isoformat()}" AND {field} < "{end.isoformat()}"',
            pageSize=100,
        )
        # Also try with no filter, to see whether the filter syntax is the problem
        # when a filtered call comes back empty.
        probe_get(
            session,
            f"list_{data_type}_nofilter",
            f"{BASE}/users/me/dataTypes/{data_type}/dataPoints",
            pageSize=10,
        )

    # dailyRollUp types. Two candidate range shapes — the docs are inconsistent,
    # so try both and record which one the API accepts.
    for data_type in ["steps", "active-zone-minutes"]:
        url = f"{BASE}/users/me/dataTypes/{data_type}/dataPoints:dailyRollUp"
        probe_post(
            session,
            f"rollup_{data_type}_iso",
            url,
            {
                "range": {
                    "startTime": f"{start.isoformat()}T00:00:00Z",
                    "endTime": f"{end.isoformat()}T00:00:00Z",
                },
                "windowSizeDays": 1,
            },
        )
        probe_post(
            session,
            f"rollup_{data_type}_civil",
            url,
            {
                "range": {
                    "start": {"year": start.year, "month": start.month, "day": start.day},
                    "end": {"year": end.year, "month": end.month, "day": end.day},
                },
                "windowSizeDays": 1,
            },
        )

    print("\nDone. Inspect spike/raw/ and write up findings.", file=sys.stderr)


if __name__ == "__main__":
    main()
```

- [ ] **Step 5: Run the spike**

```bash
source .venv/bin/activate
python spike/phase0_spike.py
```

Expected: a browser opens for consent; afterwards `spike/raw/` contains one JSON file per probe. Some probes will fail — that is the point. Do not fix failures yet; record them.

- [ ] **Step 6: Verify auth succeeded before interpreting anything else**

```bash
python -c "import json;d=json.load(open('spike/raw/profile.json'));print(d['status'])"
```

Expected: `200`. If this is `401` or `403`, the scopes or consent screen are wrong — fix that before reading any other output. A `200` here with empty data elsewhere means auth is fine and the data simply is not there yet, which is the distinction this step exists to establish.

- [ ] **Step 7: Write the findings document**

Create `docs/superpowers/plans/phase0-findings.md` with these exact headings, filled from observed output:

```markdown
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
```

- [ ] **Step 8: Reconcile findings against the plan**

If the accepted `dailyRollUp` range shape, any `filter` prefix, or any scope string differs from what Tasks 3–12 assume, update those tasks now. The findings document wins over this plan wherever they disagree — it reflects the live API.

- [ ] **Step 9: Commit**

```bash
git add .gitignore spike/ docs/superpowers/plans/phase0-findings.md
git commit -m "spike: verify Google Health API reachability and auth for Fitbit Air"
```

---

### Task 2: Package scaffolding and configuration

**Files:**
- Create: `pyproject.toml`
- Create: `src/mcp_fitbit_air/__init__.py`
- Create: `src/mcp_fitbit_air/config.py`
- Create: `tests/__init__.py`
- Create: `tests/test_config.py`

**Interfaces:**
- Consumes: nothing from earlier tasks
- Produces: `Config` dataclass with fields `client_id: str`, `client_secret: str`, `token_path: Path`; classmethod `Config.from_env() -> Config`; exception `ConfigError(Exception)`. Used by Tasks 3, 7, 13.

- [ ] **Step 1: Write `pyproject.toml`**

```toml
[project]
name = "mcp-fitbit-air"
version = "0.1.0"
description = "MCP server exposing Google Fitbit Air health data via the Google Health API"
requires-python = ">=3.11"
dependencies = [
    "mcp>=2.0.0",
    "google-auth>=2.28.0",
    "google-auth-oauthlib>=1.2.0",
    "requests>=2.31.0",
    "python-dotenv>=1.0.0",
]

[project.optional-dependencies]
dev = ["pytest>=8.0", "responses>=0.25.0"]

[project.scripts]
mcp-fitbit-air = "mcp_fitbit_air.cli:main"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/mcp_fitbit_air"]

[tool.pytest.ini_options]
testpaths = ["tests"]
```

- [ ] **Step 2: Write the failing test**

Create `tests/test_config.py`:

```python
import pytest

from mcp_fitbit_air.config import Config, ConfigError


def test_from_env_reads_required_values(monkeypatch, tmp_path):
    monkeypatch.setenv("FITBIT_MCP_CLIENT_ID", "abc123")
    monkeypatch.setenv("FITBIT_MCP_CLIENT_SECRET", "shh")
    monkeypatch.setenv("FITBIT_MCP_TOKEN_PATH", str(tmp_path / "token.json"))

    cfg = Config.from_env()

    assert cfg.client_id == "abc123"
    assert cfg.client_secret == "shh"
    assert cfg.token_path == tmp_path / "token.json"


def test_token_path_defaults_to_config_dir(monkeypatch):
    monkeypatch.setenv("FITBIT_MCP_CLIENT_ID", "abc123")
    monkeypatch.setenv("FITBIT_MCP_CLIENT_SECRET", "shh")
    monkeypatch.delenv("FITBIT_MCP_TOKEN_PATH", raising=False)

    cfg = Config.from_env()

    assert cfg.token_path.name == "token.json"
    assert "mcp-fitbit-air" in str(cfg.token_path)


def test_missing_client_id_raises_actionable_error(monkeypatch, tmp_path):
    monkeypatch.delenv("FITBIT_MCP_CLIENT_ID", raising=False)
    monkeypatch.setenv("FITBIT_MCP_CLIENT_SECRET", "shh")
    monkeypatch.chdir(tmp_path)  # no .env to fall back on

    with pytest.raises(ConfigError) as exc:
        Config.from_env()

    assert "FITBIT_MCP_CLIENT_ID" in str(exc.value)
    assert ".env" in str(exc.value)


def test_values_are_read_from_a_dotenv_file(monkeypatch, tmp_path):
    monkeypatch.delenv("FITBIT_MCP_CLIENT_ID", raising=False)
    monkeypatch.delenv("FITBIT_MCP_CLIENT_SECRET", raising=False)
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(
        "FITBIT_MCP_CLIENT_ID=from-dotenv\nFITBIT_MCP_CLIENT_SECRET=secret-from-dotenv\n"
    )

    cfg = Config.from_env()

    assert cfg.client_id == "from-dotenv"
    assert cfg.client_secret == "secret-from-dotenv"


def test_real_environment_wins_over_dotenv(monkeypatch, tmp_path):
    """Standard precedence: an explicitly exported variable beats the file, so
    Claude Desktop's `env` block overrides a stale .env left in the repo."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("FITBIT_MCP_CLIENT_ID=from-dotenv\n")
    monkeypatch.setenv("FITBIT_MCP_CLIENT_ID", "from-environment")
    monkeypatch.setenv("FITBIT_MCP_CLIENT_SECRET", "shh")

    cfg = Config.from_env()

    assert cfg.client_id == "from-environment"


def test_client_config_shape_matches_installed_app_flow(monkeypatch):
    monkeypatch.setenv("FITBIT_MCP_CLIENT_ID", "abc123")
    monkeypatch.setenv("FITBIT_MCP_CLIENT_SECRET", "shh")

    cfg = Config.from_env()

    assert cfg.client_config()["installed"]["client_id"] == "abc123"
    assert cfg.client_config()["installed"]["token_uri"].startswith("https://")
```

- [ ] **Step 3: Run the test to verify it fails**

```bash
source .venv/bin/activate
pip install -e ".[dev]"
pytest tests/test_config.py -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'mcp_fitbit_air.config'`

- [ ] **Step 4: Write the implementation**

Create `src/mcp_fitbit_air/__init__.py`:

```python
"""MCP server exposing Google Fitbit Air health data."""

__version__ = "0.1.0"
```

Create `src/mcp_fitbit_air/config.py`:

```python
"""Configuration, sourced entirely from environment variables.

Nothing here is hardcoded: this is what lets the project be open-sourced
without a refactor.

Values may also come from a .env file, which is the convenient path for local
CLI use. Real environment variables take precedence, so the `env` block in an
MCP client's config always wins over a stale .env left in the checkout.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import find_dotenv, load_dotenv

DEFAULT_TOKEN_PATH = Path.home() / ".config" / "mcp-fitbit-air" / "token.json"

# All five are required. profile/settings were absent in the Phase 0 spike's
# first round, producing 403 ACCESS_TOKEN_SCOPE_INSUFFICIENT on the profile and
# pairedDevices endpoints.
SCOPES = [
    "https://www.googleapis.com/auth/googlehealth.activity_and_fitness.readonly",
    "https://www.googleapis.com/auth/googlehealth.health_metrics_and_measurements.readonly",
    "https://www.googleapis.com/auth/googlehealth.sleep.readonly",
    "https://www.googleapis.com/auth/googlehealth.profile.readonly",
    "https://www.googleapis.com/auth/googlehealth.settings.readonly",
]


class ConfigError(Exception):
    """Raised when required configuration is missing or malformed."""


@dataclass(frozen=True)
class Config:
    client_id: str
    client_secret: str
    token_path: Path

    @classmethod
    def from_env(cls) -> "Config":
        # usecwd=True searches upward from the working directory. Bare
        # find_dotenv() would walk up from this module's own location, which
        # for an installed package is site-packages — nowhere near the user's
        # project. override=False keeps real environment variables authoritative.
        load_dotenv(find_dotenv(usecwd=True), override=False)

        client_id = os.environ.get("FITBIT_MCP_CLIENT_ID")
        client_secret = os.environ.get("FITBIT_MCP_CLIENT_SECRET")

        missing = [
            name
            for name, value in (
                ("FITBIT_MCP_CLIENT_ID", client_id),
                ("FITBIT_MCP_CLIENT_SECRET", client_secret),
            )
            if not value
        ]
        if missing:
            raise ConfigError(
                f"Missing required configuration: {', '.join(missing)}. "
                "Set these as environment variables, or copy .env.example to .env "
                "and fill them in. See the README for Google Cloud setup."
            )

        token_path_raw = os.environ.get("FITBIT_MCP_TOKEN_PATH")
        token_path = Path(token_path_raw).expanduser() if token_path_raw else DEFAULT_TOKEN_PATH

        return cls(
            client_id=client_id,
            client_secret=client_secret,
            token_path=token_path,
        )

    def client_config(self) -> dict:
        """Shape expected by InstalledAppFlow.from_client_config."""
        return {
            "installed": {
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": ["http://localhost"],
            }
        }
```

- [ ] **Step 5: Run the test to verify it passes**

```bash
pytest tests/test_config.py -v
```

Expected: PASS — 6 passed

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml src/mcp_fitbit_air/__init__.py src/mcp_fitbit_air/config.py tests/
git commit -m "feat: add package scaffolding and env-based configuration"
```

---

### Task 3: Authentication module and `auth` CLI command

**Files:**
- Create: `src/mcp_fitbit_air/auth.py`
- Create: `src/mcp_fitbit_air/cli.py`
- Create: `tests/test_auth.py`

**Interfaces:**
- Consumes: `Config`, `ConfigError`, `SCOPES` from `mcp_fitbit_air.config`
- Produces:
  - `AuthError(Exception)` with attribute `remedy: str`
  - `load_credentials(config: Config) -> google.oauth2.credentials.Credentials` — raises `AuthError` if no token file, token unreadable, or refresh fails
  - `run_auth_flow(config: Config) -> None` — interactive; writes token file at mode 0600
  - `save_credentials(creds, token_path: Path) -> None`

  Used by Tasks 6, 7.

- [ ] **Step 1: Write the failing test**

Create `tests/test_auth.py`:

```python
import json
import stat
from pathlib import Path

import pytest

from mcp_fitbit_air.auth import AuthError, load_credentials, save_credentials
from mcp_fitbit_air.config import Config


def make_config(tmp_path: Path) -> Config:
    return Config(
        client_id="abc123",
        client_secret="shh",
        token_path=tmp_path / "token.json",
    )


def test_missing_token_file_raises_with_actionable_remedy(tmp_path):
    cfg = make_config(tmp_path)

    with pytest.raises(AuthError) as exc:
        load_credentials(cfg)

    assert "mcp-fitbit-air auth" in exc.value.remedy


def test_corrupt_token_file_raises_auth_error(tmp_path):
    cfg = make_config(tmp_path)
    cfg.token_path.parent.mkdir(parents=True, exist_ok=True)
    cfg.token_path.write_text("this is not json")

    with pytest.raises(AuthError) as exc:
        load_credentials(cfg)

    assert "mcp-fitbit-air auth" in exc.value.remedy


def test_token_without_refresh_token_is_rejected(tmp_path):
    """A token with no refresh_token cannot be silently renewed, so it is
    useless to the server even if it currently has a valid access token."""
    cfg = make_config(tmp_path)
    cfg.token_path.parent.mkdir(parents=True, exist_ok=True)
    cfg.token_path.write_text(json.dumps({"token": "abc", "scopes": []}))

    with pytest.raises(AuthError) as exc:
        load_credentials(cfg)

    assert "refresh" in str(exc.value).lower()


def test_save_credentials_writes_owner_only_permissions(tmp_path):
    class FakeCreds:
        def to_json(self):
            return json.dumps({"token": "abc", "refresh_token": "r"})

    token_path = tmp_path / "nested" / "token.json"
    save_credentials(FakeCreds(), token_path)

    assert token_path.exists()
    mode = stat.S_IMODE(token_path.stat().st_mode)
    assert mode == 0o600, f"expected 0600, got {oct(mode)}"


def test_saved_token_round_trips(tmp_path):
    class FakeCreds:
        def to_json(self):
            return json.dumps({"token": "abc", "refresh_token": "r"})

    token_path = tmp_path / "token.json"
    save_credentials(FakeCreds(), token_path)

    assert json.loads(token_path.read_text())["refresh_token"] == "r"
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
pytest tests/test_auth.py -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'mcp_fitbit_air.auth'`

- [ ] **Step 3: Write `auth.py`**

Create `src/mcp_fitbit_air/auth.py`:

```python
"""Credential loading and silent refresh.

The interactive OAuth flow lives in `run_auth_flow` and is invoked ONLY by the
CLI. The MCP server never calls it: a stdio server cannot open a browser
mid-request without hanging the tool call, and anything the flow printed to
stdout would corrupt the MCP protocol stream.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

from .config import SCOPES, Config

logger = logging.getLogger(__name__)

RUN_AUTH = "Run `mcp-fitbit-air auth` to authenticate."


class AuthError(Exception):
    """Raised when credentials are missing, unusable, or unrefreshable.

    `remedy` is a user-facing instruction safe to surface through a tool result.
    """

    def __init__(self, message: str, remedy: str = RUN_AUTH) -> None:
        super().__init__(message)
        self.remedy = remedy


def save_credentials(creds, token_path: Path) -> None:
    """Write credentials to disk, readable only by the owner."""
    token_path.parent.mkdir(parents=True, exist_ok=True)
    # Create with restrictive permissions from the start, so there is no window
    # in which the token sits on disk world-readable.
    fd = os.open(token_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(creds.to_json())
    os.chmod(token_path, 0o600)


def load_credentials(config: Config) -> Credentials:
    """Load credentials, refreshing silently if the access token has expired.

    Raises AuthError with an actionable remedy on any failure.
    """
    if not config.token_path.exists():
        raise AuthError(f"No credentials found at {config.token_path}.")

    try:
        info = json.loads(config.token_path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        raise AuthError(f"Could not read credentials at {config.token_path}: {exc}") from exc

    if not info.get("refresh_token"):
        raise AuthError(
            "Stored credentials have no refresh token, so they cannot be renewed "
            "without re-authenticating."
        )

    # Client ID/secret come from the environment, not the token file, so that
    # rotating them does not require re-authentication.
    info.setdefault("client_id", config.client_id)
    info.setdefault("client_secret", config.client_secret)
    info.setdefault("token_uri", "https://oauth2.googleapis.com/token")

    try:
        creds = Credentials.from_authorized_user_info(info, scopes=SCOPES)
    except ValueError as exc:
        raise AuthError(f"Stored credentials are malformed: {exc}") from exc

    if creds.valid:
        return creds

    try:
        creds.refresh(Request())
    except RefreshError as exc:
        raise AuthError(
            "Stored credentials were rejected during refresh. The token may have "
            "been revoked, or the app may still be in 'Testing' publishing status "
            "(which expires refresh tokens after 7 days).",
            remedy=RUN_AUTH,
        ) from exc

    save_credentials(creds, config.token_path)
    logger.info("Refreshed access token")
    return creds


def run_auth_flow(config: Config) -> None:
    """Interactive OAuth. CLI only — never called from the server."""
    # Google grants only the scopes configured on the consent screen's Data
    # Access page, which can be a subset of what we request. oauthlib treats any
    # difference as fatal; relax that so we can inspect the grant and give a
    # useful message instead of a raw "Scope has changed" traceback.
    os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")

    flow = InstalledAppFlow.from_client_config(config.client_config(), scopes=SCOPES)
    creds = flow.run_local_server(port=0, access_type="offline", prompt="consent")

    granted = set(creds.scopes or [])
    missing = [s for s in SCOPES if s not in granted]
    if missing:
        raise AuthError(
            "Google did not grant these scopes: " + ", ".join(missing) + ". "
            "Scopes must be added to the OAuth consent screen's Data Access "
            "configuration, not only requested by the app.",
            remedy=(
                "Add the missing scopes at "
                "https://console.cloud.google.com/auth/scopes, then re-run "
                "`mcp-fitbit-air auth`."
            ),
        )

    if not creds.refresh_token:
        raise AuthError(
            "Google did not return a refresh token. Revoke this app's access at "
            "https://myaccount.google.com/permissions and try again.",
            remedy="Revoke access, then re-run `mcp-fitbit-air auth`.",
        )

    save_credentials(creds, config.token_path)
```

- [ ] **Step 4: Write `cli.py`**

Create `src/mcp_fitbit_air/cli.py`:

```python
"""Command line entry points.

`auth` is interactive and browser-based. `serve` starts the MCP server on
stdio and must never write to stdout.
"""

from __future__ import annotations

import argparse
import logging
import sys

from .auth import AuthError, run_auth_flow
from .config import Config, ConfigError


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mcp-fitbit-air")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("auth", help="Authenticate with Google and store a refresh token")
    sub.add_parser("serve", help="Run the MCP server on stdio")

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)

    try:
        config = Config.from_env()
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    if args.command == "auth":
        try:
            run_auth_flow(config)
        except AuthError as exc:
            print(f"Authentication failed: {exc}\n{exc.remedy}", file=sys.stderr)
            return 1
        print(f"Authenticated. Token saved to {config.token_path}", file=sys.stderr)
        return 0

    if args.command == "serve":
        from .server import run_server

        run_server()
        return 0

    return 2
```

- [ ] **Step 5: Run the tests to verify they pass**

```bash
pytest tests/test_auth.py -v
```

Expected: PASS — 5 passed

- [ ] **Step 6: Verify the CLI wiring end to end**

```bash
export FITBIT_MCP_CLIENT_ID='<your client id>'
export FITBIT_MCP_CLIENT_SECRET='<your client secret>'
mcp-fitbit-air auth
```

Expected: a browser opens, consent completes, and the terminal prints `Authenticated. Token saved to ...`. Then confirm permissions:

```bash
stat -f '%Lp' ~/.config/mcp-fitbit-air/token.json
```

Expected: `600`

- [ ] **Step 7: Commit**

```bash
git add src/mcp_fitbit_air/auth.py src/mcp_fitbit_air/cli.py tests/test_auth.py
git commit -m "feat: add credential loading, silent refresh, and auth CLI command"
```

---

### Task 4: Metric registry

The single source of truth for how each friendly metric name maps onto the API. Every later task reads from this table rather than hardcoding data type strings.

**Files:**
- Create: `src/mcp_fitbit_air/mapping.py`
- Create: `tests/test_mapping.py`

**Interfaces:**
- Consumes: nothing
- Produces:
  - `Metric` frozen dataclass: `name: str`, `data_type: str`, `method: str` (`"list"` or `"dailyRollUp"`), `unit: str`, `date_of: Callable[[dict], date | None]`, `filter_member: str`, `filter_dialect: str` (`"civil_date"` or `"physical"`), `warmup_nights: int`, `max_range_days: int`, `supports_intraday: bool`, `extract: Callable[[dict], float | None]`
  - `METRICS: dict[str, Metric]`
  - `SUMMARY_METRICS: list[str]` — the seven metrics in `get_daily_summary`
  - `get_metric(name: str) -> Metric` — raises `UnknownMetricError`
  - `coerce_number(value) -> float | None` and `dig(point, *path)` helpers
  - `UnknownMetricError(Exception)`

  Used by Tasks 6, 9, 10, 11.

Every constant in this task was verified against the live API during Phase 0.
Do not "simplify" the per-metric `filter_member` / `filter_dialect` pairs into a
uniform scheme: the dialects genuinely differ per type, and choosing the wrong
one returns HTTP 200 with zero data points rather than an error.

- [ ] **Step 1: Write the failing test**

Create `tests/test_mapping.py`:

```python
from datetime import date

import pytest

from mcp_fitbit_air.mapping import (
    METRICS,
    SUMMARY_METRICS,
    UnknownMetricError,
    coerce_number,
    get_metric,
)


def test_get_metric_returns_known_metric():
    metric = get_metric("steps")
    assert metric.data_type == "steps"
    assert metric.method == "dailyRollUp"


def test_unknown_metric_error_lists_valid_options():
    with pytest.raises(UnknownMetricError) as exc:
        get_metric("bogus")

    message = str(exc.value)
    assert "bogus" in message
    assert "steps" in message
    assert "hrv" in message


def test_all_summary_metrics_are_registered():
    for name in SUMMARY_METRICS:
        assert name in METRICS, f"{name} in SUMMARY_METRICS but not in METRICS"


def test_summary_covers_the_seven_spec_metrics():
    assert set(SUMMARY_METRICS) == {
        "sleep_duration",
        "resting_heart_rate",
        "hrv",
        "steps",
        "active_zone_minutes",
        "spo2",
        "skin_temperature_deviation",
    }


def test_every_metric_declares_a_supported_method():
    for name, metric in METRICS.items():
        assert metric.method in {"list", "dailyRollUp"}, name


def test_list_metrics_declare_a_filter_member_and_dialect():
    for name, metric in METRICS.items():
        if metric.method == "list":
            assert metric.filter_member, f"{name} uses list but declares no filter_member"
            assert metric.filter_dialect in {"civil_date", "physical"}, name


def test_heart_rate_range_limit_is_fourteen_days():
    """The API caps heart-rate queries at 14 days, unlike the 90-day default."""
    assert get_metric("heart_rate").max_range_days == 14


def test_daily_metrics_cap_at_ninety_days():
    assert get_metric("sleep_duration").max_range_days == 90
    assert get_metric("hrv").max_range_days == 90


def test_only_heart_rate_and_steps_support_intraday():
    intraday = {name for name, m in METRICS.items() if m.supports_intraday}
    assert intraday == {"heart_rate", "steps"}


# --- The Phase 0 spike proved every value below. These tests encode the real
# --- response shapes; a regression here means the API contract moved.


def test_coerce_number_handles_json_strings():
    """Integers arrive as JSON strings throughout the API."""
    assert coerce_number("8630") == 8630.0
    assert coerce_number(42) == 42.0
    assert coerce_number(35.5) == 35.5
    assert coerce_number(None) is None
    assert coerce_number("not a number") is None


def test_sleep_duration_reads_minutes_asleep_as_a_number():
    point = {"sleep": {"summary": {"minutesAsleep": "385", "minutesAwake": "5"}}}
    assert get_metric("sleep_duration").extract(point) == 385.0


def test_sleep_duration_unit_is_minutes_not_seconds():
    assert get_metric("sleep_duration").unit == "minutes"


def test_resting_heart_rate_coerces_its_string_value():
    point = {"dailyRestingHeartRate": {"beatsPerMinute": "58"}}
    assert get_metric("resting_heart_rate").extract(point) == 58.0


def test_hrv_reads_the_average_millisecond_field():
    point = {
        "dailyHeartRateVariability": {
            "averageHeartRateVariabilityMilliseconds": 42.5,
            "entropy": 1.0,
        }
    }
    assert get_metric("hrv").extract(point) == 42.5


def test_spo2_reads_average_percentage():
    point = {"dailyOxygenSaturation": {"averagePercentage": 95.8, "lowerBoundPercentage": 93.7}}
    assert get_metric("spo2").extract(point) == 95.8


def test_skin_temperature_deviation_is_nightly_minus_baseline():
    """The API returns no deviation field; it is derived."""
    point = {
        "dailySleepTemperatureDerivations": {
            "nightlyTemperatureCelsius": 34.5,
            "baselineTemperatureCelsius": 34.0,
        }
    }
    assert get_metric("skin_temperature_deviation").extract(point) == pytest.approx(0.5)


def test_skin_temperature_deviation_needs_both_halves():
    point = {"dailySleepTemperatureDerivations": {"nightlyTemperatureCelsius": 34.5}}
    assert get_metric("skin_temperature_deviation").extract(point) is None


def test_steps_reads_rollup_count_sum():
    point = {"steps": {"countSum": "8630"}}
    assert get_metric("steps").extract(point) == 8630.0


def test_active_zone_minutes_uses_fitbit_weighting():
    """Fitbit counts cardio and peak double; fat burn single."""
    point = {
        "activeZoneMinutes": {
            "sumInFatBurnHeartZone": "10",
            "sumInCardioHeartZone": "5",
            "sumInPeakHeartZone": "2",
        }
    }
    assert get_metric("active_zone_minutes").extract(point) == 10 + 2 * (5 + 2)


def test_active_zone_minutes_treats_absent_zones_as_zero():
    point = {"activeZoneMinutes": {"sumInFatBurnHeartZone": "1"}}
    assert get_metric("active_zone_minutes").extract(point) == 1.0


def test_extract_returns_none_when_the_payload_is_missing():
    for name in SUMMARY_METRICS:
        assert get_metric(name).extract({}) is None, name


def test_daily_metrics_filter_on_the_date_member():
    """daily-* points carry a `date` object, not an `interval`."""
    assert get_metric("hrv").filter_member == "daily_heart_rate_variability.date"
    assert get_metric("hrv").filter_dialect == "civil_date"


def test_sleep_filters_on_civil_end_time():
    assert get_metric("sleep_duration").filter_member == "sleep.interval.civil_end_time"
    assert get_metric("sleep_duration").filter_dialect == "civil_date"


def test_heart_rate_filters_on_physical_time():
    """Civil-time filters on heart-rate return 200 with zero points — silently
    wrong. Physical time is the only reliable dialect for this type."""
    assert get_metric("heart_rate").filter_member == "heart_rate.sample_time.physical_time"
    assert get_metric("heart_rate").filter_dialect == "physical"


def test_steps_filters_on_physical_interval_time():
    assert get_metric("steps").filter_member == "steps.interval.start_time"
    assert get_metric("steps").filter_dialect == "physical"


def test_date_of_resolves_each_type_family():
    cases = {
        "hrv": {"dailyHeartRateVariability": {"date": {"year": 2026, "month": 8, "day": 1}}},
        "steps": {"civilStartTime": {"date": {"year": 2026, "month": 8, "day": 1}}},
        "heart_rate": {
            "heartRate": {"sampleTime": {"civilTime": {"date": {"year": 2026, "month": 8, "day": 1}}}}
        },
    }
    for name, point in cases.items():
        assert get_metric(name).date_of(point) == date(2026, 8, 1), name


def test_sleep_date_comes_from_the_end_instant_plus_offset():
    """Sleep intervals carry NO civil times — only instants and offsets."""
    point = {
        "sleep": {
            "interval": {"endTime": "2026-08-02T14:50:00Z", "endUtcOffset": "-14400s"}
        }
    }
    # 14:50Z minus 4h is 10:50 local on the same day.
    assert get_metric("sleep_duration").date_of(point) == date(2026, 8, 2)


def test_sleep_date_uses_local_time_not_utc():
    """A session ending 01:30Z with a -4h offset is still the previous evening
    locally, and must be attributed to that day."""
    point = {
        "sleep": {
            "interval": {"endTime": "2026-08-03T01:30:00Z", "endUtcOffset": "-14400s"}
        }
    }
    assert get_metric("sleep_duration").date_of(point) == date(2026, 8, 2)


def test_date_of_returns_none_for_unreadable_points():
    for name in SUMMARY_METRICS:
        assert get_metric(name).date_of({}) is None, name
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
pytest tests/test_mapping.py -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'mcp_fitbit_air.mapping'`

- [ ] **Step 3: Write the implementation**

Create `src/mcp_fitbit_air/mapping.py`:

```python
"""Metric registry: friendly name -> Google Health API specifics.

Single source of truth. Every value here was verified against the live API by
the Phase 0 spike; see docs/superpowers/plans/phase0-findings.md.

Three things about this API make a naive mapping wrong:

1. Filters come in per-type dialects, and the WRONG ONE FAILS SILENTLY —
   HTTP 200 with zero data points, indistinguishable from "no data". The
   `daily-*` types filter on a `date` member; sleep filters on
   `civil_end_time`; steps and heart-rate only work with physical
   (RFC-3339) time.
2. Integers arrive as JSON strings ("8630", "61"), so every value needs
   coercion.
3. Two metrics are not returned at all and must be derived: skin temperature
   deviation (nightly minus baseline) and Active Zone Minutes (Fitbit weights
   cardio and peak double).
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any


class UnknownMetricError(Exception):
    """Raised when a caller names a metric that is not registered."""


def coerce_number(value: Any) -> float | None:
    """Coerce an API value to a finite float, or None.

    Two things make this load-bearing rather than defensive padding:

    - Integers arrive as JSON strings throughout this API ("8630", "61").
    - The API returns the literal string "NaN" for fields it cannot compute
      yet, e.g. baselineTemperatureCelsius before enough nights of history.
      Python's float("NaN") accepts that happily, and a NaN would poison every
      average it reached and serialise as bare `NaN` — which is invalid JSON,
      on a stdout stream that carries the MCP protocol.

    Returning None for non-finite values is the correct semantic: the layers
    above already render a missing value as no_data rather than inventing one.
    """
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        try:
            number = float(value)
        except ValueError:
            return None
    else:
        return None
    return number if math.isfinite(number) else None


def dig(point: dict, *path: str) -> Any:
    """Walk a nested dict, returning None if any step is missing."""
    node: Any = point
    for key in path:
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    return node


def _scalar(*path: str) -> Callable[[dict], float | None]:
    return lambda point: coerce_number(dig(point, *path))


def _civil_date_at(*path: str) -> Callable[[dict], date | None]:
    """Read a {year, month, day} object at `path`."""

    def read(point: dict) -> date | None:
        node = dig(point, *path)
        if not isinstance(node, dict):
            return None
        try:
            return date(int(node["year"]), int(node["month"]), int(node["day"]))
        except (KeyError, TypeError, ValueError):
            return None

    return read


def _offset_date_at(
    time_path: tuple[str, ...], offset_path: tuple[str, ...]
) -> Callable[[dict], date | None]:
    """Derive the local calendar date from an RFC-3339 instant plus a UTC
    offset like "-14400s".

    Sleep intervals carry no civil times at all — only startTime/endTime and
    their offsets — so the local date has to be reconstructed.
    """

    def read(point: dict) -> date | None:
        stamp = dig(point, *time_path)
        if not isinstance(stamp, str):
            return None
        try:
            moment = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        except ValueError:
            return None
        raw_offset = dig(point, *offset_path)
        seconds = 0
        if isinstance(raw_offset, str) and raw_offset.endswith("s"):
            try:
                seconds = int(raw_offset[:-1])
            except ValueError:
                seconds = 0
        return (moment + timedelta(seconds=seconds)).date()

    return read


def _skin_temperature_deviation(point: dict) -> float | None:
    """Derived: the API returns nightly and baseline, never the deviation."""
    payload = point.get("dailySleepTemperatureDerivations") or {}
    nightly = coerce_number(payload.get("nightlyTemperatureCelsius"))
    baseline = coerce_number(payload.get("baselineTemperatureCelsius"))
    if nightly is None or baseline is None:
        return None
    return round(nightly - baseline, 3)


def _active_zone_minutes(point: dict) -> float | None:
    """Fitbit's headline AZM counts cardio and peak double, fat burn single."""
    payload = point.get("activeZoneMinutes")
    if not isinstance(payload, dict):
        return None
    fat = coerce_number(payload.get("sumInFatBurnHeartZone")) or 0.0
    cardio = coerce_number(payload.get("sumInCardioHeartZone")) or 0.0
    peak = coerce_number(payload.get("sumInPeakHeartZone")) or 0.0
    return fat + 2 * (cardio + peak)


@dataclass(frozen=True)
class Metric:
    name: str
    data_type: str
    method: str  # "list" | "dailyRollUp"
    unit: str
    # AIP-160 member used for date filtering, and which literal format it needs.
    filter_member: str
    filter_dialect: str  # "civil_date" (bare YYYY-MM-DD) | "physical" (RFC-3339 Z)
    warmup_nights: int  # nights of wear before Fitbit computes this at all
    max_range_days: int
    supports_intraday: bool = False
    extract: Callable[[dict], float | None] = field(
        default=lambda _point: None, compare=False, repr=False
    )
    # Which local calendar day a data point belongs to. Not a single dotted
    # path: daily-* types carry a civil {year,month,day}, rollups carry one at
    # the top level, and sleep carries only an instant plus a UTC offset.
    date_of: Callable[[dict], "date | None"] = field(
        default=lambda _point: None, compare=False, repr=False
    )


METRICS: dict[str, Metric] = {
    "sleep_duration": Metric(
        name="sleep_duration",
        data_type="sleep",
        method="list",
        unit="minutes",  # sleep.summary.minutesAsleep is minutes, not seconds
        filter_member="sleep.interval.civil_end_time",
        filter_dialect="civil_date",
        warmup_nights=0,
        max_range_days=90,
        extract=_scalar("sleep", "summary", "minutesAsleep"),
        # Sleep intervals have no civil times; derive the day from the
        # end instant plus its offset, so a session is attributed to the
        # morning you woke up.
        date_of=_offset_date_at(
            ("sleep", "interval", "endTime"), ("sleep", "interval", "endUtcOffset")
        ),
    ),
    "resting_heart_rate": Metric(
        name="resting_heart_rate",
        data_type="daily-resting-heart-rate",
        method="list",
        unit="bpm",
        filter_member="daily_resting_heart_rate.date",
        filter_dialect="civil_date",
        warmup_nights=1,
        max_range_days=90,
        extract=_scalar("dailyRestingHeartRate", "beatsPerMinute"),
        date_of=_civil_date_at("dailyRestingHeartRate", "date"),
    ),
    "hrv": Metric(
        name="hrv",
        data_type="daily-heart-rate-variability",
        method="list",
        unit="milliseconds",
        filter_member="daily_heart_rate_variability.date",
        filter_dialect="civil_date",
        warmup_nights=3,
        max_range_days=90,
        extract=_scalar(
            "dailyHeartRateVariability", "averageHeartRateVariabilityMilliseconds"
        ),
        date_of=_civil_date_at("dailyHeartRateVariability", "date"),
    ),
    "spo2": Metric(
        name="spo2",
        data_type="daily-oxygen-saturation",
        method="list",
        unit="percent",
        filter_member="daily_oxygen_saturation.date",
        filter_dialect="civil_date",
        warmup_nights=1,
        max_range_days=90,
        extract=_scalar("dailyOxygenSaturation", "averagePercentage"),
        date_of=_civil_date_at("dailyOxygenSaturation", "date"),
    ),
    "skin_temperature_deviation": Metric(
        name="skin_temperature_deviation",
        data_type="daily-sleep-temperature-derivations",
        method="list",
        unit="celsius_deviation",
        filter_member="daily_sleep_temperature_derivations.date",
        filter_dialect="civil_date",
        warmup_nights=3,
        max_range_days=90,
        extract=_skin_temperature_deviation,
        date_of=_civil_date_at("dailySleepTemperatureDerivations", "date"),
    ),
    "steps": Metric(
        name="steps",
        data_type="steps",
        method="dailyRollUp",
        unit="count",
        filter_member="steps.interval.start_time",
        filter_dialect="physical",  # civil filters return 0 points, silently
        warmup_nights=0,
        max_range_days=90,
        supports_intraday=True,
        extract=_scalar("steps", "countSum"),
        # Rollup points carry civilStartTime at the top level.
        date_of=_civil_date_at("civilStartTime", "date"),
    ),
    "active_zone_minutes": Metric(
        name="active_zone_minutes",
        data_type="active-zone-minutes",
        method="dailyRollUp",
        unit="minutes",
        filter_member="active_zone_minutes.interval.start_time",
        filter_dialect="physical",
        warmup_nights=0,
        max_range_days=90,
        extract=_active_zone_minutes,
        date_of=_civil_date_at("civilStartTime", "date"),
    ),
    "heart_rate": Metric(
        name="heart_rate",
        data_type="heart-rate",
        method="dailyRollUp",
        unit="bpm",
        filter_member="heart_rate.sample_time.physical_time",
        filter_dialect="physical",
        warmup_nights=0,
        # The API caps heart-rate queries at 14 days, unlike the 90-day default.
        max_range_days=14,
        supports_intraday=True,
        extract=_scalar("heartRate", "beatsPerMinute"),
        date_of=_civil_date_at("heartRate", "sampleTime", "civilTime", "date"),
    ),
}

# The columns returned by get_daily_summary, in display order.
SUMMARY_METRICS: list[str] = [
    "sleep_duration",
    "resting_heart_rate",
    "hrv",
    "steps",
    "active_zone_minutes",
    "spo2",
    "skin_temperature_deviation",
]


def get_metric(name: str) -> Metric:
    try:
        return METRICS[name]
    except KeyError:
        valid = ", ".join(sorted(METRICS))
        raise UnknownMetricError(
            f"Unknown metric {name!r}. Valid metrics are: {valid}"
        ) from None
```

- [ ] **Step 4: Run the test to verify it passes**

```bash
pytest tests/test_mapping.py -v
```

Expected: PASS — 10 passed

- [ ] **Step 5: Commit**

```bash
git add src/mcp_fitbit_air/mapping.py tests/test_mapping.py
git commit -m "feat: add metric registry mapping friendly names to Health API types"
```

---

### Task 5: Date parsing and result contract

Two small pure modules, built together because neither is large enough to warrant its own review gate and the result contract has no dependencies.

**Files:**
- Create: `src/mcp_fitbit_air/dates.py`
- Create: `src/mcp_fitbit_air/results.py`
- Create: `tests/test_dates.py`
- Create: `tests/test_results.py`

**Interfaces:**
- Consumes: nothing
- Produces:
  - `dates.resolve_range(start: str, end: str | None, tz: ZoneInfo, today: date | None = None) -> tuple[date, date]` — inclusive start, inclusive end
  - `dates.resolve_day(value: str, tz: ZoneInfo, today: date | None = None) -> date`
  - `dates.DateParseError(Exception)`
  - `results.ResultState` — str enum with `OK`, `NO_DATA`, `WARMING_UP`, `ERROR`
  - `results.ToolResult` dataclass with `.to_dict()`, and constructors `ToolResult.ok(data, **meta)`, `.no_data(message, **meta)`, `.warming_up(message, **meta)`, `.error(message, remedy=None, **meta)`

  Used by Tasks 7, 9, 10, 11, 12.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_dates.py`:

```python
from datetime import date
from zoneinfo import ZoneInfo

import pytest

from mcp_fitbit_air.dates import DateParseError, resolve_day, resolve_range

TZ = ZoneInfo("America/New_York")
TODAY = date(2026, 8, 2)  # a Sunday


def test_iso_dates_pass_through():
    assert resolve_range("2026-07-01", "2026-07-05", TZ, TODAY) == (
        date(2026, 7, 1),
        date(2026, 7, 5),
    )


def test_yesterday():
    assert resolve_day("yesterday", TZ, TODAY) == date(2026, 8, 1)


def test_today():
    assert resolve_day("today", TZ, TODAY) == TODAY


def test_last_week_is_the_seven_days_ending_yesterday():
    start, end = resolve_range("last week", None, TZ, TODAY)
    assert (start, end) == (date(2026, 7, 26), date(2026, 8, 1))


def test_last_n_days():
    start, end = resolve_range("last 30 days", None, TZ, TODAY)
    assert (start, end) == (date(2026, 7, 3), date(2026, 8, 1))


def test_last_month():
    start, end = resolve_range("last month", None, TZ, TODAY)
    assert (start, end) == (date(2026, 7, 3), date(2026, 8, 1))


def test_range_spanning_a_month_boundary():
    assert resolve_range("2026-07-28", "2026-08-02", TZ, TODAY) == (
        date(2026, 7, 28),
        date(2026, 8, 2),
    )


def test_omitted_end_with_iso_start_runs_through_today():
    start, end = resolve_range("2026-07-28", None, TZ, TODAY)
    assert (start, end) == (date(2026, 7, 28), TODAY)


def test_reversed_range_raises():
    with pytest.raises(DateParseError) as exc:
        resolve_range("2026-08-02", "2026-07-01", TZ, TODAY)
    assert "before" in str(exc.value).lower()


def test_unparseable_input_raises_with_examples():
    with pytest.raises(DateParseError) as exc:
        resolve_day("the day before the big meeting", TZ, TODAY)
    assert "yesterday" in str(exc.value)


def test_parsing_is_case_and_whitespace_insensitive():
    assert resolve_day("  YESTERDAY ", TZ, TODAY) == date(2026, 8, 1)
```

Create `tests/test_results.py`:

```python
from mcp_fitbit_air.results import ResultState, ToolResult


def test_ok_result_serialises_state_and_data():
    result = ToolResult.ok({"steps": 1000})
    assert result.to_dict() == {"state": "ok", "data": {"steps": 1000}}


def test_no_data_is_not_an_error():
    result = ToolResult.no_data("No data recorded for 2026-08-01.")
    payload = result.to_dict()
    assert payload["state"] == "no_data"
    assert payload["message"].startswith("No data")
    assert "data" not in payload


def test_warming_up_carries_nights_so_far():
    result = ToolResult.warming_up(
        "HRV needs 3 nights of wear before Fitbit computes it.", nights_so_far=1
    )
    payload = result.to_dict()
    assert payload["state"] == "warming_up"
    assert payload["nights_so_far"] == 1


def test_error_includes_remedy_when_given():
    result = ToolResult.error("Not authenticated.", remedy="Run `mcp-fitbit-air auth`.")
    payload = result.to_dict()
    assert payload["state"] == "error"
    assert payload["remedy"] == "Run `mcp-fitbit-air auth`."


def test_meta_keys_are_merged_into_the_payload():
    result = ToolResult.ok([1, 2, 3], truncated=True, reason="7-day intraday cap")
    payload = result.to_dict()
    assert payload["truncated"] is True
    assert payload["reason"] == "7-day intraday cap"


def test_states_are_plain_strings_for_json_serialisation():
    assert ResultState.OK == "ok"
    assert ResultState.WARMING_UP.value == "warming_up"
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
pytest tests/test_dates.py tests/test_results.py -v
```

Expected: FAIL — `ModuleNotFoundError` for both modules

- [ ] **Step 3: Write `dates.py`**

Create `src/mcp_fitbit_air/dates.py`:

```python
"""Natural-language date resolution.

Claude should never have to do date arithmetic before calling a tool — that is
a reliable source of off-by-one errors, especially around "this week". All
relative expressions resolve against today in the user's profile timezone.

Relative ranges end YESTERDAY, not today: today's data is incomplete and
including a partial day would skew any comparison against it.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

EXAMPLES = 'Try "yesterday", "last week", "last 30 days", or "2026-07-28".'


class DateParseError(Exception):
    """Raised when a date expression cannot be understood."""


def _today_in(tz: ZoneInfo, today: date | None) -> date:
    return today if today is not None else datetime.now(tz).date()


def _parse_iso(value: str) -> date | None:
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def resolve_day(value: str, tz: ZoneInfo, today: date | None = None) -> date:
    """Resolve a single-day expression to a concrete date."""
    text = value.strip().lower()
    now = _today_in(tz, today)

    if text == "today":
        return now
    if text == "yesterday":
        return now - timedelta(days=1)

    parsed = _parse_iso(text)
    if parsed is not None:
        return parsed

    match = re.fullmatch(r"(\d+) days? ago", text)
    if match:
        return now - timedelta(days=int(match.group(1)))

    raise DateParseError(f"Could not understand the date {value!r}. {EXAMPLES}")


def _relative_range(text: str, now: date) -> tuple[date, date] | None:
    """Ranges ending yesterday, since today is always partial."""
    end = now - timedelta(days=1)

    if text in {"last week", "past week", "this week"}:
        return end - timedelta(days=6), end
    if text in {"last month", "past month"}:
        return end - timedelta(days=29), end

    match = re.fullmatch(r"last (\d+) days?", text)
    if match:
        days = int(match.group(1))
        if days < 1:
            return None
        return end - timedelta(days=days - 1), end

    return None


def resolve_range(
    start: str,
    end: str | None,
    tz: ZoneInfo,
    today: date | None = None,
) -> tuple[date, date]:
    """Resolve a date range. Both bounds are inclusive.

    `start` may itself be a whole-range expression such as "last week", in which
    case `end` must be omitted.
    """
    now = _today_in(tz, today)
    text = start.strip().lower()

    relative = _relative_range(text, now)
    if relative is not None:
        if end is not None:
            raise DateParseError(
                f"{start!r} already describes a whole range, so an end date cannot "
                "also be given."
            )
        return relative

    start_date = resolve_day(start, tz, today)
    end_date = resolve_day(end, tz, today) if end is not None else now

    if end_date < start_date:
        raise DateParseError(
            f"End date {end_date.isoformat()} is before start date "
            f"{start_date.isoformat()}."
        )

    return start_date, end_date
```

- [ ] **Step 4: Write `results.py`**

Create `src/mcp_fitbit_air/results.py`:

```python
"""The tool response contract.

Every tool returns one of four states. Empty results are NEVER collapsed into a
bare empty list: with an account only days old, "no data" is a common and
legitimate answer, and it must be distinguishable from "broken".

`no_data` and `warming_up` are not errors.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ResultState(str, Enum):
    OK = "ok"
    NO_DATA = "no_data"
    WARMING_UP = "warming_up"
    ERROR = "error"


@dataclass
class ToolResult:
    state: ResultState
    data: Any | None = None
    message: str | None = None
    remedy: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def ok(cls, data: Any, **meta: Any) -> "ToolResult":
        return cls(state=ResultState.OK, data=data, meta=meta)

    @classmethod
    def no_data(cls, message: str, **meta: Any) -> "ToolResult":
        return cls(state=ResultState.NO_DATA, message=message, meta=meta)

    @classmethod
    def warming_up(cls, message: str, **meta: Any) -> "ToolResult":
        return cls(state=ResultState.WARMING_UP, message=message, meta=meta)

    @classmethod
    def error(cls, message: str, remedy: str | None = None, **meta: Any) -> "ToolResult":
        return cls(state=ResultState.ERROR, message=message, remedy=remedy, meta=meta)

    def to_dict(self) -> dict[str, Any]:
        """Serialise for the MCP stream.

        Meta goes down FIRST and the authoritative fields are written over it.
        `state` is not a named parameter on any constructor, so without this
        ordering a caller could pass state="ok" as a meta kwarg and have an
        error serialise as a success — defeating the one guarantee this module
        exists to provide.
        """
        payload: dict[str, Any] = dict(self.meta)
        payload["state"] = self.state.value
        if self.data is not None:
            payload["data"] = self.data
        if self.message is not None:
            payload["message"] = self.message
        if self.remedy is not None:
            payload["remedy"] = self.remedy
        return payload
```

- [ ] **Step 5: Run the tests to verify they pass**

```bash
pytest tests/test_dates.py tests/test_results.py -v
```

Expected: PASS — 17 passed

- [ ] **Step 6: Commit**

```bash
git add src/mcp_fitbit_air/dates.py src/mcp_fitbit_air/results.py tests/test_dates.py tests/test_results.py
git commit -m "feat: add natural-language date parsing and four-state result contract"
```

---

### Task 6: HTTP client with retry and pagination

**Files:**
- Create: `src/mcp_fitbit_air/client.py`
- Create: `tests/conftest.py`
- Create: `tests/fixtures/` (populated from spike output)
- Create: `scripts/capture_fixtures.py`
- Create: `tests/test_client.py`

**Interfaces:**
- Consumes: `AuthError` from `auth`, `Metric` from `mapping`
- Produces:
  - `HealthClient(credentials)` with methods:
    - `.get_profile() -> dict`
    - `.get_settings() -> dict` (timezone lives here, not in profile)
    - `.get_paired_devices() -> list[dict]`
    - `.list_data_points(data_type: str, filter_expr: str | None = None, page_size: int = 1440) -> list[dict]`
    - `.daily_rollup(data_type: str, start: date, end: date, window_size_days: int = 1) -> list[dict]`
  - `ApiError(Exception)` with attributes `status: int | None`, `remedy: str | None`
  - `RateLimitError(ApiError)`

  Used by Tasks 7, 9, 10, 11, 12.

- [ ] **Step 1: Write the fixture capture script**

Create `scripts/capture_fixtures.py`:

```python
"""Convert raw spike output into scrubbed test fixtures.

Fixtures are real health data. This script strips identifiers and shifts dates
onto a fixed synthetic baseline before anything is committed.
"""

from __future__ import annotations

import json
import re
import sys
from datetime import date, timedelta
from pathlib import Path

RAW = Path("spike/raw")
OUT = Path("tests/fixtures")

# All real dates are shifted so that the most recent becomes this date.
ANCHOR = date(2026, 1, 8)

ID_KEYS = {"userId", "user", "name", "deviceId", "serialNumber", "dataSource", "macAddress"}
DATE_RE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")


def collect_dates(node) -> list[date]:
    found: list[date] = []
    text = json.dumps(node)
    for match in DATE_RE.finditer(text):
        try:
            found.append(date.fromisoformat(match.group(0)))
        except ValueError:
            continue
    return found


def scrub(node, shift: timedelta):
    if isinstance(node, dict):
        out = {}
        for key, value in node.items():
            if key in ID_KEYS and isinstance(value, str):
                out[key] = f"redacted-{key}"
            else:
                out[key] = scrub(value, shift)
        return out
    if isinstance(node, list):
        return [scrub(item, shift) for item in node]
    if isinstance(node, str):
        def replace(match: re.Match) -> str:
            try:
                shifted = date.fromisoformat(match.group(0)) + shift
            except ValueError:
                return match.group(0)
            return shifted.isoformat()

        return DATE_RE.sub(replace, node)
    return node


def main() -> int:
    if not RAW.exists():
        print(f"No raw spike output at {RAW}. Run the Phase 0 spike first.", file=sys.stderr)
        return 1

    OUT.mkdir(parents=True, exist_ok=True)
    all_dates: list[date] = []
    for path in RAW.glob("*.json"):
        all_dates.extend(collect_dates(json.loads(path.read_text())))

    shift = ANCHOR - max(all_dates) if all_dates else timedelta(0)

    for path in RAW.glob("*.json"):
        payload = json.loads(path.read_text())
        scrubbed = scrub(payload, shift)
        (OUT / path.name).write_text(json.dumps(scrubbed, indent=2, sort_keys=True))
        print(f"wrote {OUT / path.name}", file=sys.stderr)

    print(f"\nShifted all dates by {shift.days} days. REVIEW THE OUTPUT BEFORE COMMITTING.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 2: Generate the fixtures and review them by hand**

```bash
python scripts/capture_fixtures.py
```

Then **read every file** in `tests/fixtures/` and confirm no real identifiers survived. This is a manual gate — the scrubber handles known key names, but real API responses may carry identifiers under names it does not know. Delete any file whose contents you are unsure about.

- [ ] **Step 3: Write `tests/conftest.py`**

```python
import json
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def load_fixture():
    """Return the `body` of a captured spike response."""

    def _load(name: str):
        path = FIXTURES / f"{name}.json"
        if not path.exists():
            pytest.skip(f"fixture {name}.json not captured")
        return json.loads(path.read_text())["body"]

    return _load


@pytest.fixture
def fake_credentials():
    """Credentials stand-in. AuthorizedSession is bypassed in client tests via
    the `session` injection point, so these need no real token machinery."""

    class FakeCreds:
        valid = True
        token = "fake-token"

        def refresh(self, request):  # pragma: no cover - never called when valid
            raise AssertionError("refresh should not be called on valid credentials")

    return FakeCreds()
```

- [ ] **Step 4: Write the failing test**

Create `tests/test_client.py`:

```python
import json
from datetime import date

import pytest
import responses

from mcp_fitbit_air.client import ApiError, HealthClient, RateLimitError

BASE = "https://health.googleapis.com/v4"


@pytest.fixture
def client(fake_credentials):
    import requests

    return HealthClient(fake_credentials, session=requests.Session())


@responses.activate
def test_get_profile_returns_body(client):
    responses.add(
        responses.GET,
        f"{BASE}/users/me/profile",
        json={"displayName": "Test", "timezone": "America/New_York"},
        status=200,
    )

    profile = client.get_profile()

    assert profile["timezone"] == "America/New_York"


@responses.activate
def test_list_data_points_follows_pagination(client):
    responses.add(
        responses.GET,
        f"{BASE}/users/me/dataTypes/sleep/dataPoints",
        json={"dataPoints": [{"id": 1}], "nextPageToken": "page2"},
        status=200,
    )
    responses.add(
        responses.GET,
        f"{BASE}/users/me/dataTypes/sleep/dataPoints",
        json={"dataPoints": [{"id": 2}]},
        status=200,
    )

    points = client.list_data_points("sleep")

    assert [p["id"] for p in points] == [1, 2]
    assert len(responses.calls) == 2


@responses.activate
def test_list_data_points_sends_filter_expression(client):
    responses.add(
        responses.GET,
        f"{BASE}/users/me/dataTypes/sleep/dataPoints",
        json={"dataPoints": []},
        status=200,
    )

    client.list_data_points("sleep", filter_expr='sleep.interval.civil_end_time >= "2026-08-01"')

    assert "filter=" in responses.calls[0].request.url


@responses.activate
def test_empty_data_points_returns_empty_list_not_error(client):
    responses.add(
        responses.GET,
        f"{BASE}/users/me/dataTypes/daily-heart-rate-variability/dataPoints",
        json={},
        status=200,
    )

    assert client.list_data_points("daily-heart-rate-variability") == []


@responses.activate
def test_daily_rollup_posts_range_and_returns_points(client):
    responses.add(
        responses.POST,
        f"{BASE}/users/me/dataTypes/steps/dataPoints:dailyRollUp",
        json={"rollupDataPoints": [{"steps": {"count": 900}}]},
        status=200,
    )

    points = client.daily_rollup("steps", date(2026, 8, 1), date(2026, 8, 2))

    assert points[0]["steps"]["count"] == 900
    body = json.loads(responses.calls[0].request.body.decode())
    # Nested CivilDateTime. Phase 0 proved ISO strings and bare year/month/day
    # are both rejected with HTTP 400.
    assert body["range"]["start"] == {"date": {"year": 2026, "month": 8, "day": 1}}
    # Range is closed-open, so the inclusive end of Aug 2 is sent as Aug 3.
    assert body["range"]["end"] == {"date": {"year": 2026, "month": 8, "day": 3}}
    assert body["windowSizeDays"] == 1


@responses.activate
def test_get_settings_returns_timezone(client):
    """Timezone lives in settings; profile has no timezone field at all."""
    responses.add(
        responses.GET,
        f"{BASE}/users/me/settings",
        json={"timeZone": "America/New_York", "utcOffset": "-14400s"},
        status=200,
    )

    assert client.get_settings()["timeZone"] == "America/New_York"


@responses.activate
def test_401_raises_api_error_with_auth_remedy(client):
    responses.add(
        responses.GET,
        f"{BASE}/users/me/profile",
        json={"error": {"message": "Invalid Credentials"}},
        status=401,
    )

    with pytest.raises(ApiError) as exc:
        client.get_profile()

    assert exc.value.status == 401
    assert "mcp-fitbit-air auth" in (exc.value.remedy or "")


@responses.activate
def test_429_retries_then_raises_rate_limit_error(client, monkeypatch):
    monkeypatch.setattr("mcp_fitbit_air.client.time.sleep", lambda _: None)
    for _ in range(4):
        responses.add(
            responses.GET,
            f"{BASE}/users/me/profile",
            json={"error": {"message": "Too many requests"}},
            status=429,
        )

    with pytest.raises(RateLimitError):
        client.get_profile()

    assert len(responses.calls) == 4  # initial attempt plus 3 retries


@responses.activate
def test_429_then_success_succeeds(client, monkeypatch):
    monkeypatch.setattr("mcp_fitbit_air.client.time.sleep", lambda _: None)
    responses.add(responses.GET, f"{BASE}/users/me/profile", json={}, status=429)
    responses.add(
        responses.GET, f"{BASE}/users/me/profile", json={"timezone": "UTC"}, status=200
    )

    assert client.get_profile()["timezone"] == "UTC"


@responses.activate
def test_500_surfaces_server_message(client, monkeypatch):
    monkeypatch.setattr("mcp_fitbit_air.client.time.sleep", lambda _: None)
    for _ in range(4):
        responses.add(
            responses.GET,
            f"{BASE}/users/me/profile",
            json={"error": {"message": "Backend error"}},
            status=500,
        )

    with pytest.raises(ApiError) as exc:
        client.get_profile()

    assert "Backend error" in str(exc.value)
```

- [ ] **Step 5: Run the test to verify it fails**

```bash
pytest tests/test_client.py -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'mcp_fitbit_air.client'`

- [ ] **Step 6: Write the implementation**

Create `src/mcp_fitbit_air/client.py`:

```python
"""All HTTP against the Google Health API.

This module is the single seam through which API access flows. Adding a local
cache or sync layer later means changing this file only — no tool signature
anywhere else needs to change.
"""

from __future__ import annotations

import logging
import time
from datetime import date, timedelta
from typing import Any

import requests
from google.auth.transport.requests import AuthorizedSession

logger = logging.getLogger(__name__)


def _civil_date(value: date) -> dict[str, int]:
    return {"year": value.year, "month": value.month, "day": value.day}

BASE_URL = "https://health.googleapis.com/v4"
MAX_RETRIES = 3
BACKOFF_BASE_SECONDS = 1.0
TIMEOUT_SECONDS = 30
RETRYABLE_STATUSES = {429, 500, 502, 503, 504}

AUTH_REMEDY = "Run `mcp-fitbit-air auth` to re-authenticate."


class ApiError(Exception):
    def __init__(
        self, message: str, status: int | None = None, remedy: str | None = None
    ) -> None:
        super().__init__(message)
        self.status = status
        self.remedy = remedy


class RateLimitError(ApiError):
    """Raised when the API keeps returning 429 after the retry budget."""


class HealthClient:
    def __init__(self, credentials, session=None) -> None:
        # `session` is injectable so tests can drive a plain requests.Session
        # without real credential machinery.
        self._session = session or AuthorizedSession(credentials)
        self._credentials = credentials

    # -- internals ---------------------------------------------------------

    def _request(self, method: str, url: str, **kwargs) -> dict[str, Any]:
        last_error: Exception | None = None

        for attempt in range(MAX_RETRIES + 1):
            try:
                response = self._session.request(
                    method, url, timeout=TIMEOUT_SECONDS, **kwargs
                )
            except requests.RequestException as exc:
                last_error = ApiError(f"Network error contacting the Health API: {exc}")
                if attempt < MAX_RETRIES:
                    time.sleep(BACKOFF_BASE_SECONDS * (2**attempt))
                    continue
                raise last_error from exc

            if response.status_code in RETRYABLE_STATUSES and attempt < MAX_RETRIES:
                delay = BACKOFF_BASE_SECONDS * (2**attempt)
                logger.warning(
                    "HTTP %s from %s; retrying in %.1fs", response.status_code, url, delay
                )
                time.sleep(delay)
                continue

            return self._handle(response)

        raise last_error or ApiError("Request failed after retries")

    def _handle(self, response) -> dict[str, Any]:
        if response.ok:
            return response.json() if response.content else {}

        message = self._extract_message(response)
        status = response.status_code

        if status in (401, 403):
            raise ApiError(
                f"The Health API rejected our credentials ({status}): {message}",
                status=status,
                remedy=AUTH_REMEDY,
            )
        if status == 429:
            raise RateLimitError(
                f"Rate limited by the Health API: {message}",
                status=status,
                remedy="Wait a few minutes and try again.",
            )
        raise ApiError(f"Health API error ({status}): {message}", status=status)

    @staticmethod
    def _extract_message(response) -> str:
        try:
            payload = response.json()
        except ValueError:
            return response.text[:200] or "no response body"
        if isinstance(payload, dict):
            error = payload.get("error")
            if isinstance(error, dict):
                return error.get("message", str(error))
            if error:
                return str(error)
        return str(payload)[:200]

    # -- public API --------------------------------------------------------

    def get_profile(self) -> dict[str, Any]:
        return self._request("GET", f"{BASE_URL}/users/me/profile")

    def get_settings(self) -> dict[str, Any]:
        """Settings, not profile, is where `timeZone` and `utcOffset` live."""
        return self._request("GET", f"{BASE_URL}/users/me/settings")

    def get_paired_devices(self) -> list[dict[str, Any]]:
        payload = self._request("GET", f"{BASE_URL}/users/me/pairedDevices")
        return payload.get("pairedDevices", []) or payload.get("devices", [])

    def list_data_points(
        self,
        data_type: str,
        filter_expr: str | None = None,
        page_size: int = 1440,
    ) -> list[dict[str, Any]]:
        """List data points, following pagination to completion."""
        url = f"{BASE_URL}/users/me/dataTypes/{data_type}/dataPoints"
        params: dict[str, Any] = {"pageSize": page_size}
        if filter_expr:
            params["filter"] = filter_expr

        collected: list[dict[str, Any]] = []
        while True:
            payload = self._request("GET", url, params=params)
            collected.extend(payload.get("dataPoints", []))
            token = payload.get("nextPageToken")
            if not token:
                return collected
            params = dict(params, pageToken=token)

    def daily_rollup(
        self,
        data_type: str,
        start: date,
        end: date,
        window_size_days: int = 1,
    ) -> list[dict[str, Any]]:
        """Roll data up into per-day buckets. `end` is inclusive here; the API
        range is closed-open, so we send end + 1 day.

        `range.start` / `range.end` are nested CivilDateTime objects. Phase 0
        verified that ISO `startTime`/`endTime` strings and bare
        `{year, month, day}` are both rejected with HTTP 400.
        """
        url = f"{BASE_URL}/users/me/dataTypes/{data_type}/dataPoints:dailyRollUp"
        exclusive_end = end + timedelta(days=1)
        body = {
            "range": {
                "start": {"date": _civil_date(start)},
                "end": {"date": _civil_date(exclusive_end)},
            },
            "windowSizeDays": window_size_days,
        }

        collected: list[dict[str, Any]] = []
        while True:
            payload = self._request("POST", url, json=body)
            collected.extend(payload.get("rollupDataPoints", []))
            token = payload.get("nextPageToken")
            if not token:
                return collected
            body = dict(body, pageToken=token)
```

- [ ] **Step 7: Run the test to verify it passes**

```bash
pytest tests/test_client.py -v
```

Expected: PASS — 9 passed

- [ ] **Step 8: Commit**

```bash
git add src/mcp_fitbit_air/client.py scripts/capture_fixtures.py tests/conftest.py tests/fixtures tests/test_client.py
git commit -m "feat: add Health API client with retry, pagination, and scrubbed fixtures"
```

---

### Task 7: Server bootstrap and `get_profile_and_devices`

The first working tool, and the end-to-end smoke test for everything below it.

**Files:**
- Create: `src/mcp_fitbit_air/server.py`
- Create: `src/mcp_fitbit_air/context.py`
- Create: `tests/test_server_profile.py`

**Interfaces:**
- Consumes: `Config` (Task 2), `load_credentials`/`AuthError` (Task 3), `HealthClient`/`ApiError` (Task 6), `ToolResult` (Task 5)
- Produces:
  - `context.ServerContext` with `.client -> HealthClient` (lazily built), `.timezone -> ZoneInfo` (cached from `settings.timeZone`, defaulting to UTC)
  - `context.get_context() -> ServerContext`
  - `server.mcp` — the `MCPServer` instance
  - `server.run_server() -> None`
  - `server.get_profile_and_devices()` tool
  - `server.tool_guard` decorator translating `AuthError` / `ApiError` / `UnknownMetricError` / `DateParseError` into `ToolResult.error` payloads

  Used by Tasks 9, 10, 11, 12.

- [ ] **Step 1: Write the failing test**

Create `tests/test_server_profile.py`:

```python
from unittest.mock import Mock

import pytest

from mcp_fitbit_air.auth import AuthError
from mcp_fitbit_air.client import ApiError, RateLimitError


@pytest.fixture
def fake_context(monkeypatch):
    ctx = Mock()
    monkeypatch.setattr("mcp_fitbit_air.server.get_context", lambda: ctx)
    return ctx


def test_profile_and_devices_returns_ok_with_battery(fake_context):
    from mcp_fitbit_air.server import get_profile_and_devices

    fake_context.client.get_profile.return_value = {"age": 30}
    fake_context.client.get_settings.return_value = {"timeZone": "America/New_York"}
    fake_context.client.get_paired_devices.return_value = [
        {"deviceVersion": "Fitbit Air", "batteryLevel": 82, "lastSyncTime": "2026-08-02T09:00:00Z"}
    ]

    result = get_profile_and_devices()

    assert result["state"] == "ok"
    assert result["data"]["devices"][0]["batteryLevel"] == 82
    # Timezone comes from settings; the profile response has no such field.
    assert result["data"]["settings"]["timeZone"] == "America/New_York"


def test_no_paired_devices_is_no_data_not_error(fake_context):
    from mcp_fitbit_air.server import get_profile_and_devices

    fake_context.client.get_profile.return_value = {"age": 30}
    fake_context.client.get_settings.return_value = {"timeZone": "UTC"}
    fake_context.client.get_paired_devices.return_value = []

    result = get_profile_and_devices()

    assert result["state"] == "no_data"
    assert "device" in result["message"].lower()


def test_auth_error_becomes_error_result_with_remedy(fake_context):
    from mcp_fitbit_air.server import get_profile_and_devices

    type(fake_context).client = property(
        lambda self: (_ for _ in ()).throw(AuthError("no token"))
    )

    result = get_profile_and_devices()

    assert result["state"] == "error"
    assert "mcp-fitbit-air auth" in result["remedy"]


def test_api_error_becomes_error_result(fake_context):
    from mcp_fitbit_air.server import get_profile_and_devices

    fake_context.client.get_profile.side_effect = ApiError("boom", status=500)

    result = get_profile_and_devices()

    assert result["state"] == "error"
    assert "boom" in result["message"]


def test_rate_limit_surfaces_wait_remedy(fake_context):
    from mcp_fitbit_air.server import get_profile_and_devices

    fake_context.client.get_profile.side_effect = RateLimitError(
        "slow down", status=429, remedy="Wait a few minutes and try again."
    )

    result = get_profile_and_devices()

    assert result["state"] == "error"
    assert "Wait a few minutes" in result["remedy"]


def test_tool_never_raises_out_of_the_tool_boundary(fake_context):
    """An unexpected exception must still return a structured error, because a
    raised exception inside an MCP tool is far less useful to Claude."""
    from mcp_fitbit_air.server import get_profile_and_devices

    fake_context.client.get_profile.side_effect = ValueError("unexpected")

    result = get_profile_and_devices()

    assert result["state"] == "error"
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
pytest tests/test_server_profile.py -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'mcp_fitbit_air.server'`

- [ ] **Step 3: Write `context.py`**

Create `src/mcp_fitbit_air/context.py`:

```python
"""Lazily built, process-wide server state.

Credentials are loaded on first use rather than at import time, so that a
missing token produces a clean error result from a tool call instead of
crashing the server before it can speak MCP.
"""

from __future__ import annotations

import logging
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .auth import load_credentials
from .client import ApiError, HealthClient
from .config import Config

logger = logging.getLogger(__name__)

DEFAULT_TIMEZONE = ZoneInfo("UTC")


class ServerContext:
    def __init__(self) -> None:
        self._client: HealthClient | None = None
        self._timezone: ZoneInfo | None = None

    @property
    def client(self) -> HealthClient:
        if self._client is None:
            config = Config.from_env()
            credentials = load_credentials(config)
            self._client = HealthClient(credentials)
        return self._client

    @property
    def timezone(self) -> ZoneInfo:
        """The user's timezone, used to resolve relative dates and to convert
        local dates into the UTC instants that physical-time filters need.

        This comes from `users/me/settings`, NOT the profile — Phase 0 confirmed
        the profile response carries no timezone field of any kind.
        """
        if self._timezone is None:
            try:
                name = self.client.get_settings().get("timeZone")
                self._timezone = ZoneInfo(name) if name else DEFAULT_TIMEZONE
            except (ApiError, KeyError, ValueError, ZoneInfoNotFoundError) as exc:
                logger.warning("Falling back to UTC; could not read settings timeZone: %s", exc)
                self._timezone = DEFAULT_TIMEZONE
        return self._timezone


_context: ServerContext | None = None


def get_context() -> ServerContext:
    global _context
    if _context is None:
        _context = ServerContext()
    return _context
```

- [ ] **Step 4: Write `server.py`**

Create `src/mcp_fitbit_air/server.py`:

```python
"""MCP server exposing Fitbit Air data.

Never write to stdout from this process: stdout is the MCP protocol stream.
Diagnostics go to stderr via logging.
"""

from __future__ import annotations

import functools
import logging
import sys
from typing import Any, Callable

from mcp.server.mcpserver import MCPServer

from .auth import AuthError
from .client import ApiError
from .config import ConfigError
from .context import get_context
from .dates import DateParseError
from .mapping import UnknownMetricError
from .results import ToolResult

logger = logging.getLogger(__name__)

mcp = MCPServer("fitbit-air")


def tool_guard(fn: Callable[..., ToolResult]) -> Callable[..., dict[str, Any]]:
    """Translate exceptions into structured error results.

    A tool must never raise out of its boundary: a structured error result tells
    Claude what went wrong and how to fix it, where a traceback does not.
    """

    @functools.wraps(fn)
    def wrapper(*args, **kwargs) -> dict[str, Any]:
        try:
            return fn(*args, **kwargs).to_dict()
        except AuthError as exc:
            return ToolResult.error(str(exc), remedy=exc.remedy).to_dict()
        except ConfigError as exc:
            return ToolResult.error(
                str(exc), remedy="Set the required environment variables; see the README."
            ).to_dict()
        except ApiError as exc:
            return ToolResult.error(str(exc), remedy=exc.remedy).to_dict()
        except (UnknownMetricError, DateParseError) as exc:
            return ToolResult.error(str(exc)).to_dict()
        except Exception as exc:  # noqa: BLE001 - deliberate boundary
            logger.exception("Unexpected error in %s", fn.__name__)
            return ToolResult.error(f"Unexpected error in {fn.__name__}: {exc}").to_dict()

    return wrapper


@mcp.tool()
@tool_guard
def get_profile_and_devices() -> ToolResult:
    """Get the user's profile, settings, and paired Fitbit devices.

    Returns age and membership date, unit and timezone settings, and each paired
    device's battery level and last sync time. Useful on its own for "is my
    Fitbit synced?", and a good first call when other tools return no data — a
    stale lastSyncTime explains missing data better than any other signal.
    """
    ctx = get_context()
    profile = ctx.client.get_profile()
    settings = ctx.client.get_settings()
    devices = ctx.client.get_paired_devices()

    if not devices:
        return ToolResult.no_data(
            "No paired devices found on this account. If the Fitbit Air was set up "
            "recently, open the Google Health app and confirm it has synced at least once.",
            profile=profile,
            settings=settings,
        )

    return ToolResult.ok({"profile": profile, "settings": settings, "devices": devices})


def run_server() -> None:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    mcp.run()
```

- [ ] **Step 5: Run the test to verify it passes**

```bash
pytest tests/test_server_profile.py -v
```

Expected: PASS — 6 passed

- [ ] **Step 6: Verify the server starts and speaks MCP**

```bash
pytest tests/ -v
mcp-fitbit-air serve < /dev/null
```

Expected: the server starts, receives EOF on stdin, and exits without a traceback. Nothing may be printed to stdout.

- [ ] **Step 7: Commit**

```bash
git add src/mcp_fitbit_air/server.py src/mcp_fitbit_air/context.py tests/test_server_profile.py
git commit -m "feat: add MCP server bootstrap and get_profile_and_devices tool"
```

---

### Task 8: Baseline computation

**Files:**
- Create: `src/mcp_fitbit_air/baselines.py`
- Create: `tests/test_baselines.py`

**Interfaces:**
- Consumes: nothing
- Produces:
  - `Baseline` frozen dataclass: `mean: float | None`, `n: int`, `window_days: int`, with `.to_dict()`
  - `compute_baseline(values: list[float | None], window_days: int = 30) -> Baseline`
  - `BASELINE_WINDOW_DAYS = 30`

  Used by Tasks 9, 10.

- [ ] **Step 1: Write the failing test**

Create `tests/test_baselines.py`:

```python
from mcp_fitbit_air.baselines import Baseline, compute_baseline


def test_mean_of_values():
    baseline = compute_baseline([10.0, 20.0, 30.0])
    assert baseline.mean == 20.0
    assert baseline.n == 3


def test_none_values_are_excluded_from_the_mean():
    baseline = compute_baseline([10.0, None, 30.0])
    assert baseline.mean == 20.0
    assert baseline.n == 2


def test_empty_input_yields_no_mean():
    baseline = compute_baseline([])
    assert baseline.mean is None
    assert baseline.n == 0


def test_all_none_yields_no_mean():
    baseline = compute_baseline([None, None])
    assert baseline.mean is None
    assert baseline.n == 0


def test_sample_size_is_reported_so_small_samples_are_not_over_read():
    """With an account days old, a 4-day mean must not look like a 30-day one."""
    baseline = compute_baseline([50.0, 52.0, 48.0, 51.0])
    payload = baseline.to_dict()
    assert payload["n"] == 4
    assert payload["window_days"] == 30


def test_mean_is_rounded_to_one_decimal():
    baseline = compute_baseline([1.0, 2.0])
    assert baseline.mean == 1.5
    baseline = compute_baseline([1.0, 1.0, 2.0])
    assert baseline.mean == 1.3
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
pytest tests/test_baselines.py -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'mcp_fitbit_air.baselines'`

- [ ] **Step 3: Write the implementation**

Create `src/mcp_fitbit_air/baselines.py`:

```python
"""Trailing baselines.

"HRV 42ms" is noise; "HRV 42ms against a 58ms baseline" is a finding. Every
baseline carries its sample size, so that a 4-day mean from a new account is
not mistaken for a settled 30-day one.
"""

from __future__ import annotations

from dataclasses import dataclass

BASELINE_WINDOW_DAYS = 30


@dataclass(frozen=True)
class Baseline:
    mean: float | None
    n: int
    window_days: int

    def to_dict(self) -> dict:
        return {"mean": self.mean, "n": self.n, "window_days": self.window_days}


def compute_baseline(
    values: list[float | None], window_days: int = BASELINE_WINDOW_DAYS
) -> Baseline:
    present = [v for v in values if v is not None]
    if not present:
        return Baseline(mean=None, n=0, window_days=window_days)
    return Baseline(
        mean=round(sum(present) / len(present), 1),
        n=len(present),
        window_days=window_days,
    )
```

- [ ] **Step 4: Run the test to verify it passes**

```bash
pytest tests/test_baselines.py -v
```

Expected: PASS — 6 passed

- [ ] **Step 5: Commit**

```bash
git add src/mcp_fitbit_air/baselines.py tests/test_baselines.py
git commit -m "feat: add trailing baseline computation with sample-size reporting"
```

---

### Task 9: Metric fetching and per-day normalisation

The layer between the raw client and the summary tool: fetch one metric over a range, and reduce it to one value per day.

**Files:**
- Create: `src/mcp_fitbit_air/fetch.py`
- Create: `tests/test_fetch.py`

**Interfaces:**
- Consumes: `Metric`/`get_metric` (Task 4), `HealthClient`/`ApiError` (Task 6), `ResultState` (Task 5)
- Produces:
  - `MetricSeries` dataclass: `metric: Metric`, `by_day: dict[date, float]`, `state: ResultState`, `message: str | None`
  - `fetch_metric(client, metric_name: str, start: date, end: date, tz: ZoneInfo) -> MetricSeries`
  - `build_filter(metric: Metric, start: date, end: date, tz: ZoneInfo) -> str`
  - `point_date(metric: Metric, point: dict) -> date | None`

  Used by Tasks 10, 11.

- [ ] **Step 1: Write the failing test**

Create `tests/test_fetch.py`:

```python
from datetime import date
from zoneinfo import ZoneInfo
from unittest.mock import Mock

from mcp_fitbit_air.client import ApiError
from mcp_fitbit_air.fetch import build_filter, fetch_metric, point_date
from mcp_fitbit_air.mapping import get_metric
from mcp_fitbit_air.results import ResultState

START = date(2026, 8, 1)
END = date(2026, 8, 3)
TZ = ZoneInfo("America/New_York")


def daily_point(payload_key, fields, day=(2026, 8, 1)):
    y, m, d = day
    return {payload_key: {"date": {"year": y, "month": m, "day": d}, **fields}}


def test_list_metric_maps_points_onto_days():
    client = Mock()
    client.list_data_points.return_value = [
        daily_point("dailyRestingHeartRate", {"beatsPerMinute": "58"}, (2026, 8, 1)),
        daily_point("dailyRestingHeartRate", {"beatsPerMinute": "60"}, (2026, 8, 2)),
    ]

    series = fetch_metric(client, "resting_heart_rate", START, END, TZ)

    assert series.state is ResultState.OK
    assert series.by_day[date(2026, 8, 1)] == 58.0
    assert series.by_day[date(2026, 8, 2)] == 60.0


def test_rollup_metric_uses_daily_rollup_call():
    client = Mock()
    client.daily_rollup.return_value = [
        {
            "civilStartTime": {"date": {"year": 2026, "month": 8, "day": 1}},
            "steps": {"countSum": "9000"},
        }
    ]

    series = fetch_metric(client, "steps", START, END, TZ)

    client.daily_rollup.assert_called_once()
    client.list_data_points.assert_not_called()
    assert series.by_day[date(2026, 8, 1)] == 9000.0


def test_empty_response_for_zero_warmup_metric_is_no_data():
    client = Mock()
    client.daily_rollup.return_value = []

    series = fetch_metric(client, "steps", START, END, TZ)

    assert series.state is ResultState.NO_DATA
    assert series.by_day == {}


def test_empty_response_for_warmup_metric_is_warming_up():
    """HRV needs several nights. An empty result over a short window means the
    metric has not been computed yet, not that anything is broken."""
    client = Mock()
    client.list_data_points.return_value = []

    series = fetch_metric(client, "hrv", date(2026, 8, 1), date(2026, 8, 2), TZ)

    assert series.state is ResultState.WARMING_UP
    assert "3" in series.message


def test_empty_response_over_a_long_window_is_no_data_not_warming_up():
    client = Mock()
    client.list_data_points.return_value = []

    series = fetch_metric(client, "hrv", date(2026, 6, 1), date(2026, 8, 1), TZ)

    assert series.state is ResultState.NO_DATA


def test_api_error_becomes_error_state_rather_than_raising():
    client = Mock()
    client.list_data_points.side_effect = ApiError("boom", status=500)

    series = fetch_metric(client, "hrv", START, END, TZ)

    assert series.state is ResultState.ERROR
    assert "boom" in series.message


def test_sleep_sums_multiple_sessions_in_one_day():
    """A nap and a night's sleep on the same day must combine, not overwrite."""
    client = Mock()
    point = lambda mins: {
        "sleep": {
            "interval": {"endTime": "2026-08-01T12:00:00Z", "endUtcOffset": "-14400s"},
            "summary": {"minutesAsleep": mins},
        }
    }
    client.list_data_points.return_value = [point("385"), point("45")]

    series = fetch_metric(client, "sleep_duration", START, END, TZ)

    assert series.by_day[date(2026, 8, 1)] == 430.0


def test_non_additive_metric_keeps_one_value_per_day():
    client = Mock()
    client.list_data_points.return_value = [
        daily_point("dailyRestingHeartRate", {"beatsPerMinute": "58"}, (2026, 8, 1)),
        daily_point("dailyRestingHeartRate", {"beatsPerMinute": "62"}, (2026, 8, 1)),
    ]

    series = fetch_metric(client, "resting_heart_rate", START, END, TZ)

    assert series.by_day[date(2026, 8, 1)] == 62.0


def test_skin_temperature_deviation_is_derived_per_day():
    client = Mock()
    client.list_data_points.return_value = [
        daily_point(
            "dailySleepTemperatureDerivations",
            {"nightlyTemperatureCelsius": 34.5, "baselineTemperatureCelsius": 34.0},
            (2026, 8, 1),
        )
    ]

    series = fetch_metric(client, "skin_temperature_deviation", START, END, TZ)

    assert series.by_day[date(2026, 8, 1)] == 0.5


def test_unreadable_points_are_skipped_not_fatal():
    client = Mock()
    client.list_data_points.return_value = [
        {"garbage": True},
        daily_point("dailyRestingHeartRate", {"beatsPerMinute": "58"}, (2026, 8, 1)),
    ]

    series = fetch_metric(client, "resting_heart_rate", START, END, TZ)

    assert series.by_day == {date(2026, 8, 1): 58.0}


def test_point_date_delegates_to_the_metric_resolver():
    assert point_date(
        get_metric("hrv"),
        {"dailyHeartRateVariability": {"date": {"year": 2026, "month": 8, "day": 1}}},
    ) == date(2026, 8, 1)
    assert point_date(
        get_metric("steps"),
        {"civilStartTime": {"date": {"year": 2026, "month": 8, "day": 2}}},
    ) == date(2026, 8, 2)
    assert point_date(get_metric("hrv"), {"nothing": 1}) is None


# --- Filter construction. Getting the dialect wrong returns HTTP 200 with zero
# --- points rather than an error, so these assertions are load-bearing.


def test_civil_date_filter_uses_bare_dates_and_an_exclusive_end():
    expr = build_filter(get_metric("hrv"), START, END, TZ)
    assert expr == (
        'daily_heart_rate_variability.date >= "2026-08-01" AND '
        'daily_heart_rate_variability.date < "2026-08-04"'
    )


def test_sleep_filter_uses_civil_end_time():
    expr = build_filter(get_metric("sleep_duration"), START, END, TZ)
    assert expr.startswith('sleep.interval.civil_end_time >= "2026-08-01"')


def test_physical_filter_uses_rfc3339_utc_instants():
    """Local midnight in America/New_York is 04:00Z in August."""
    expr = build_filter(get_metric("heart_rate"), START, START, TZ)
    assert expr == (
        'heart_rate.sample_time.physical_time >= "2026-08-01T04:00:00Z" AND '
        'heart_rate.sample_time.physical_time < "2026-08-02T04:00:00Z"'
    )


def test_physical_filter_respects_a_different_timezone():
    expr = build_filter(get_metric("heart_rate"), START, START, ZoneInfo("UTC"))
    assert '"2026-08-01T00:00:00Z"' in expr
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
pytest tests/test_fetch.py -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'mcp_fitbit_air.fetch'`

- [ ] **Step 3: Write the implementation**

Create `src/mcp_fitbit_air/fetch.py`:

```python
"""Fetch one metric over a date range and reduce it to one value per day.

Every shape here was verified against the live API during Phase 0. Two details
matter more than they look:

- Filter dialects differ per data type and the wrong one FAILS SILENTLY,
  returning HTTP 200 with zero points. `build_filter` is the only place that
  decides, and its tests pin the exact strings.
- Physical-time filters compare against UTC instants, so local dates must be
  converted through the user's timezone. Getting this wrong shifts every
  intraday query by the UTC offset.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from .client import ApiError
from .mapping import Metric, get_metric
from .results import ResultState

logger = logging.getLogger(__name__)

# Metrics whose same-day values should be summed rather than overwritten.
ADDITIVE_UNITS = {"minutes", "count"}


@dataclass
class MetricSeries:
    metric: Metric
    by_day: dict[date, float] = field(default_factory=dict)
    state: ResultState = ResultState.OK
    message: str | None = None


def point_date(metric: Metric, point: dict) -> date | None:
    """Read this point's local calendar day.

    Delegates to the metric's own resolver: the date lives in a different place
    for every type family, and sleep has to derive it from an instant plus an
    offset because its interval carries no civil times.
    """
    return metric.date_of(point)


def _utc_instant(day: date, tz: ZoneInfo) -> str:
    """Local midnight on `day`, expressed as an RFC-3339 UTC instant."""
    local = datetime.combine(day, time.min, tzinfo=tz)
    return local.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_filter(metric: Metric, start: date, end: date, tz: ZoneInfo) -> str:
    """Build the AIP-160 filter for this metric. `end` is inclusive; the API
    range is closed-open, so the emitted upper bound is the following day."""
    exclusive_end = end + timedelta(days=1)
    member = metric.filter_member

    if metric.filter_dialect == "physical":
        lower = _utc_instant(start, tz)
        upper = _utc_instant(exclusive_end, tz)
    else:  # "civil_date"
        lower = start.isoformat()
        upper = exclusive_end.isoformat()

    return f'{member} >= "{lower}" AND {member} < "{upper}"'


def _accumulate(series: MetricSeries, points: list[dict]) -> None:
    additive = series.metric.unit in ADDITIVE_UNITS
    for point in points:
        day = point_date(series.metric, point)
        value = series.metric.extract(point)
        if day is None or value is None:
            continue
        if additive and day in series.by_day:
            series.by_day[day] += value
        else:
            series.by_day[day] = value


def fetch_metric(
    client, metric_name: str, start: date, end: date, tz: ZoneInfo
) -> MetricSeries:
    """Fetch one metric. Never raises for API problems — returns ERROR state."""
    metric = get_metric(metric_name)
    series = MetricSeries(metric=metric)

    try:
        if metric.method == "dailyRollUp":
            points = client.daily_rollup(metric.data_type, start, end)
        else:
            points = client.list_data_points(
                metric.data_type, filter_expr=build_filter(metric, start, end, tz)
            )
    except ApiError as exc:
        series.state = ResultState.ERROR
        series.message = str(exc)
        return series

    _accumulate(series, points)

    if not series.by_day:
        window_days = (end - start).days + 1
        if metric.warmup_nights and window_days <= metric.warmup_nights * 2:
            series.state = ResultState.WARMING_UP
            series.message = (
                f"{metric.name} needs about {metric.warmup_nights} nights of wear "
                "before Fitbit computes it. No values yet for this range."
            )
        else:
            series.state = ResultState.NO_DATA
            series.message = (
                f"No {metric.name} recorded between {start.isoformat()} and "
                f"{end.isoformat()}. The band may not have been worn, or may not "
                "have synced."
            )

    return series
```

- [ ] **Step 4: Run the test to verify it passes**

```bash
pytest tests/test_fetch.py -v
```

Expected: PASS — 8 passed

- [ ] **Step 5: Commit**

```bash
git add src/mcp_fitbit_air/fetch.py tests/test_fetch.py
git commit -m "feat: add per-metric fetching with per-day normalisation and warm-up detection"
```

---

### Task 10: `get_daily_summary` tool

The workhorse. One call should answer most questions.

**Files:**
- Modify: `src/mcp_fitbit_air/server.py` (append the tool)
- Create: `src/mcp_fitbit_air/summary.py`
- Create: `tests/test_summary.py`

**Interfaces:**
- Consumes: `fetch_metric`/`MetricSeries` (Task 9), `SUMMARY_METRICS`/`get_metric` (Task 4), `compute_baseline` (Task 8), `resolve_range` (Task 5), `ToolResult` (Task 5), `get_context` (Task 7)
- Produces:
  - `summary.build_summary(client, start: date, end: date, metric_names: list[str], tz: ZoneInfo) -> dict`
  - `summary.MAX_SUMMARY_DAYS = 90`
  - `server.get_daily_summary(start_date: str, end_date: str | None = None)` tool

- [ ] **Step 1: Write the failing test**

Create `tests/test_summary.py`:

```python
from datetime import date
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

import pytest

from mcp_fitbit_air.results import ResultState
from mcp_fitbit_air.summary import MAX_SUMMARY_DAYS, build_summary
from mcp_fitbit_air.fetch import MetricSeries
from mcp_fitbit_air.mapping import get_metric


TZ = ZoneInfo("UTC")


def series(name, by_day, state=ResultState.OK, message=None):
    return MetricSeries(
        metric=get_metric(name), by_day=by_day, state=state, message=message
    )


def test_summary_has_one_row_per_day_in_range():
    fake = {
        "steps": series("steps", {date(2026, 8, 1): 9000, date(2026, 8, 2): 11000}),
    }
    with patch("mcp_fitbit_air.summary.fetch_metric", side_effect=lambda c, n, s, e, tz: fake[n]):
        result = build_summary(Mock(), date(2026, 8, 1), date(2026, 8, 3), ["steps"], TZ)

    assert [row["date"] for row in result["days"]] == ["2026-08-01", "2026-08-02", "2026-08-03"]


def test_values_carry_units_and_baselines():
    fake = {"steps": series("steps", {date(2026, 8, 1): 9000, date(2026, 8, 2): 11000})}
    with patch("mcp_fitbit_air.summary.fetch_metric", side_effect=lambda c, n, s, e, tz: fake[n]):
        result = build_summary(Mock(), date(2026, 8, 1), date(2026, 8, 2), ["steps"], TZ)

    cell = result["days"][0]["metrics"]["steps"]
    assert cell["value"] == 9000
    assert cell["unit"] == "count"
    assert cell["baseline"]["mean"] == 10000.0
    assert cell["baseline"]["n"] == 2


def test_day_with_no_value_is_marked_no_data_not_zero():
    fake = {"steps": series("steps", {date(2026, 8, 1): 9000})}
    with patch("mcp_fitbit_air.summary.fetch_metric", side_effect=lambda c, n, s, e, tz: fake[n]):
        result = build_summary(Mock(), date(2026, 8, 1), date(2026, 8, 2), ["steps"], TZ)

    cell = result["days"][1]["metrics"]["steps"]
    assert cell["state"] == "no_data"
    assert "value" not in cell


def test_one_failing_metric_does_not_sink_the_others():
    fake = {
        "steps": series("steps", {date(2026, 8, 1): 9000}),
        "hrv": series("hrv", {}, state=ResultState.ERROR, message="boom"),
    }
    with patch("mcp_fitbit_air.summary.fetch_metric", side_effect=lambda c, n, s, e, tz: fake[n]):
        result = build_summary(Mock(), date(2026, 8, 1), date(2026, 8, 1), ["steps", "hrv"], TZ)

    metrics = result["days"][0]["metrics"]
    assert metrics["steps"]["value"] == 9000
    assert metrics["hrv"]["state"] == "error"


def test_warming_up_metric_is_reported_per_day():
    fake = {"hrv": series("hrv", {}, state=ResultState.WARMING_UP, message="needs 3 nights")}
    with patch("mcp_fitbit_air.summary.fetch_metric", side_effect=lambda c, n, s, e, tz: fake[n]):
        result = build_summary(Mock(), date(2026, 8, 1), date(2026, 8, 1), ["hrv"], TZ)

    cell = result["days"][0]["metrics"]["hrv"]
    assert cell["state"] == "warming_up"
    assert "3 nights" in cell["message"]


def test_metric_level_problems_are_summarised_at_the_top():
    fake = {
        "steps": series("steps", {date(2026, 8, 1): 9000}),
        "hrv": series("hrv", {}, state=ResultState.ERROR, message="boom"),
    }
    with patch("mcp_fitbit_air.summary.fetch_metric", side_effect=lambda c, n, s, e, tz: fake[n]):
        result = build_summary(Mock(), date(2026, 8, 1), date(2026, 8, 1), ["steps", "hrv"], TZ)

    assert result["metric_status"]["hrv"]["state"] == "error"
    assert result["metric_status"]["steps"]["state"] == "ok"


def test_range_longer_than_the_cap_is_rejected():
    with pytest.raises(ValueError) as exc:
        build_summary(Mock(), date(2026, 1, 1), date(2026, 12, 31), ["steps"], TZ)
    assert str(MAX_SUMMARY_DAYS) in str(exc.value)
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
pytest tests/test_summary.py -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'mcp_fitbit_air.summary'`

- [ ] **Step 3: Write `summary.py`**

Create `src/mcp_fitbit_air/summary.py`:

```python
"""Build the per-day summary table.

Metrics are fetched concurrently: seven sequential round trips would make the
most common tool call the slowest one.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from zoneinfo import ZoneInfo

from .baselines import compute_baseline
from .fetch import MetricSeries, fetch_metric
from .mapping import get_metric
from .results import ResultState

logger = logging.getLogger(__name__)

MAX_SUMMARY_DAYS = 90


def _days_in(start: date, end: date) -> list[date]:
    return [start + timedelta(days=offset) for offset in range((end - start).days + 1)]


def build_summary(
    client, start: date, end: date, metric_names: list[str], tz: ZoneInfo
) -> dict:
    span = (end - start).days + 1
    if span > MAX_SUMMARY_DAYS:
        raise ValueError(
            f"Range of {span} days exceeds the {MAX_SUMMARY_DAYS}-day maximum for a "
            "daily summary. Request a narrower range, or use get_metric_series for a "
            "single metric."
        )

    with ThreadPoolExecutor(max_workers=len(metric_names) or 1) as pool:
        series_list: list[MetricSeries] = list(
            pool.map(lambda name: fetch_metric(client, name, start, end, tz), metric_names)
        )
    series_by_name = {s.metric.name: s for s in series_list}

    baselines = {
        name: compute_baseline(list(s.by_day.values()))
        for name, s in series_by_name.items()
    }

    days = []
    for day in _days_in(start, end):
        row: dict = {"date": day.isoformat(), "metrics": {}}
        for name in metric_names:
            series = series_by_name[name]
            metric = series.metric

            if series.state is ResultState.ERROR:
                row["metrics"][name] = {"state": "error", "message": series.message}
                continue

            value = series.by_day.get(day)
            if value is None:
                if series.state is ResultState.WARMING_UP:
                    row["metrics"][name] = {
                        "state": "warming_up",
                        "message": series.message,
                    }
                else:
                    row["metrics"][name] = {
                        "state": "no_data",
                        "message": f"No {name} recorded for {day.isoformat()}.",
                    }
                continue

            row["metrics"][name] = {
                "state": "ok",
                "value": value,
                "unit": metric.unit,
                "baseline": baselines[name].to_dict(),
            }
        days.append(row)

    metric_status = {
        name: {"state": s.state.value, "message": s.message}
        for name, s in series_by_name.items()
    }

    return {
        "range": {"start": start.isoformat(), "end": end.isoformat()},
        "days": days,
        "metric_status": metric_status,
    }
```

- [ ] **Step 4: Append the tool to `server.py`**

Add these imports to the existing import block in `src/mcp_fitbit_air/server.py`:

```python
from .dates import resolve_range
from .mapping import SUMMARY_METRICS
from .summary import build_summary
```

Then append:

```python
@mcp.tool()
@tool_guard
def get_daily_summary(start_date: str, end_date: str | None = None) -> ToolResult:
    """Get a day-by-day health summary: sleep, resting heart rate, HRV, steps,
    active zone minutes, SpO2, and skin temperature deviation.

    This is the tool to reach for first — most questions about recent health
    can be answered from a single call.

    Dates accept natural language: "last week", "yesterday", "last 30 days",
    "2026-07-28". Give a whole-range expression as start_date on its own
    ("last week"), or a start and end pair. Maximum range is 90 days.

    Every value carries its unit and a trailing 30-day baseline with the sample
    size it was computed from — a baseline with a small n should not be read as
    a settled norm. Days with no data are marked no_data rather than zero, and
    metrics Fitbit has not computed yet are marked warming_up.
    """
    ctx = get_context()
    start, end = resolve_range(start_date, end_date, ctx.timezone)
    try:
        summary = build_summary(ctx.client, start, end, SUMMARY_METRICS, ctx.timezone)
    except ValueError as exc:
        return ToolResult.error(str(exc))

    has_any = any(
        cell.get("state") == "ok"
        for day in summary["days"]
        for cell in day["metrics"].values()
    )
    if not has_any:
        return ToolResult.no_data(
            f"No health data recorded between {start.isoformat()} and {end.isoformat()}. "
            "Check that the Fitbit Air has synced recently with get_profile_and_devices.",
            **summary,
        )

    return ToolResult.ok(summary)
```

- [ ] **Step 5: Run the tests to verify they pass**

```bash
pytest tests/test_summary.py -v
```

Expected: PASS — 7 passed

- [ ] **Step 6: Run the whole suite**

```bash
pytest tests/ -v
```

Expected: PASS — all tests

- [ ] **Step 7: Commit**

```bash
git add src/mcp_fitbit_air/summary.py src/mcp_fitbit_air/server.py tests/test_summary.py
git commit -m "feat: add get_daily_summary tool with concurrent fan-out and baselines"
```

---

### Task 11: `get_metric_series` tool

**Files:**
- Modify: `src/mcp_fitbit_air/server.py` (append the tool)
- Create: `tests/test_metric_series.py`

**Interfaces:**
- Consumes: `fetch_metric` (Task 9), `get_metric`/`METRICS` (Task 4), `compute_baseline` (Task 8), `resolve_range` (Task 5), `HealthClient.list_data_points` (Task 6)
- Produces: `server.get_metric_series(metric: str, start_date: str, end_date: str | None = None, granularity: str = "daily")` tool; module constant `server.MAX_INTRADAY_DAYS = 7`

- [ ] **Step 1: Write the failing test**

Create `tests/test_metric_series.py`:

```python
from datetime import date
from unittest.mock import Mock

import pytest

from mcp_fitbit_air.fetch import MetricSeries
from mcp_fitbit_air.mapping import get_metric
from mcp_fitbit_air.results import ResultState


@pytest.fixture
def fake_context(monkeypatch):
    ctx = Mock()
    ctx.timezone = __import__("zoneinfo").ZoneInfo("UTC")
    monkeypatch.setattr("mcp_fitbit_air.server.get_context", lambda: ctx)
    return ctx


def test_daily_series_returns_points_with_baseline(fake_context, monkeypatch):
    from mcp_fitbit_air import server

    monkeypatch.setattr(
        server,
        "fetch_metric",
        lambda c, n, s, e, tz: MetricSeries(
            metric=get_metric("hrv"),
            by_day={date(2026, 8, 1): 55.0, date(2026, 8, 2): 42.0},
        ),
    )

    result = server.get_metric_series("hrv", "2026-08-01", "2026-08-02")

    assert result["state"] == "ok"
    assert result["data"]["unit"] == "milliseconds"
    assert result["data"]["baseline"]["mean"] == 48.5
    assert len(result["data"]["points"]) == 2


def test_unknown_metric_returns_error_listing_valid_names(fake_context):
    from mcp_fitbit_air import server

    result = server.get_metric_series("bogus", "2026-08-01")

    assert result["state"] == "error"
    assert "hrv" in result["message"]


def test_intraday_on_unsupported_metric_is_an_error(fake_context):
    from mcp_fitbit_air import server

    result = server.get_metric_series("hrv", "2026-08-01", granularity="intraday")

    assert result["state"] == "error"
    assert "intraday" in result["message"].lower()


def test_intraday_range_is_truncated_to_seven_days(fake_context):
    from mcp_fitbit_air import server

    fake_context.client.list_data_points.return_value = [
        {"heartRate": {"bpm": 62}, "interval": {"startTime": "2026-08-01T09:00:00Z"}}
    ]

    result = server.get_metric_series(
        "heart_rate", "2026-07-01", "2026-08-01", granularity="intraday"
    )

    assert result["truncated"] is True
    assert "7" in result["reason"]


def test_invalid_granularity_is_rejected(fake_context):
    from mcp_fitbit_air import server

    result = server.get_metric_series("hrv", "2026-08-01", granularity="hourly")

    assert result["state"] == "error"
    assert "daily" in result["message"]


def test_warming_up_state_is_propagated(fake_context, monkeypatch):
    from mcp_fitbit_air import server

    monkeypatch.setattr(
        server,
        "fetch_metric",
        lambda c, n, s, e, tz: MetricSeries(
            metric=get_metric("hrv"),
            by_day={},
            state=ResultState.WARMING_UP,
            message="needs 3 nights",
        ),
    )

    result = server.get_metric_series("hrv", "2026-08-01", "2026-08-02")

    assert result["state"] == "warming_up"
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
pytest tests/test_metric_series.py -v
```

Expected: FAIL — `AttributeError: module 'mcp_fitbit_air.server' has no attribute 'get_metric_series'`

- [ ] **Step 3: Append the tool to `server.py`**

Add to the existing imports in `src/mcp_fitbit_air/server.py`:

```python
import datetime as dt
from datetime import timedelta

from .baselines import compute_baseline
from .fetch import build_filter, fetch_metric
from .mapping import METRICS, get_metric
from .results import ResultState
```

Import `datetime as dt` rather than `from datetime import date`: the
`get_sleep_detail` tool added in Task 12 takes a parameter named `date`, and a
module-level `date` import would be shadowed inside it.

Then append:

```python
MAX_INTRADAY_DAYS = 7


def _reading_time(point: dict) -> str | None:
    """Best-effort physical timestamp for one intraday reading."""
    for holder in (point.get("heartRate") or {}, point.get("steps") or {}, point):
        if not isinstance(holder, dict):
            continue
        sample = holder.get("sampleTime")
        if isinstance(sample, dict) and isinstance(sample.get("physicalTime"), str):
            return sample["physicalTime"]
        interval = holder.get("interval")
        if isinstance(interval, dict) and isinstance(interval.get("startTime"), str):
            return interval["startTime"]
    return None


def _intraday_series(ctx, metric, start: dt.date, end: dt.date, truncated: bool) -> ToolResult:
    # build_filter owns the per-type dialect choice; the wrong one returns
    # HTTP 200 with zero points rather than an error.
    points = ctx.client.list_data_points(
        metric.data_type,
        filter_expr=build_filter(metric, start, end, ctx.timezone),
    )

    readings = []
    for point in points:
        value = metric.extract(point)
        if value is None:
            continue
        readings.append({"time": _reading_time(point), "value": value})

    meta = {}
    if truncated:
        meta = {
            "truncated": True,
            "reason": (
                f"Intraday requests are capped at {MAX_INTRADAY_DAYS} days to keep "
                "responses manageable; only the most recent window was returned."
            ),
        }

    if not readings:
        return ToolResult.no_data(
            f"No intraday {metric.name} recorded between {start.isoformat()} and "
            f"{end.isoformat()}.",
            **meta,
        )

    return ToolResult.ok(
        {
            "metric": metric.name,
            "unit": metric.unit,
            "granularity": "intraday",
            "range": {"start": start.isoformat(), "end": end.isoformat()},
            "readings": readings,
        },
        **meta,
    )


@mcp.tool()
@tool_guard
def get_metric_series(
    metric: str,
    start_date: str,
    end_date: str | None = None,
    granularity: str = "daily",
) -> ToolResult:
    """Get one metric over time, for drilling into a trend.

    Valid metrics: sleep_duration, resting_heart_rate, hrv, steps,
    active_zone_minutes, spo2, skin_temperature_deviation, heart_rate.

    granularity is "daily" (default) or "intraday". Intraday gives
    minute-level readings and is supported only for heart_rate and steps; it is
    capped at 7 days regardless of the range requested, and the response says
    so when it truncates.

    Dates accept natural language, as in get_daily_summary. Daily results carry
    a trailing 30-day baseline with its sample size.
    """
    if granularity not in {"daily", "intraday"}:
        return ToolResult.error(
            f"Unknown granularity {granularity!r}. Use \"daily\" or \"intraday\"."
        )

    ctx = get_context()
    spec = get_metric(metric)  # raises UnknownMetricError, handled by tool_guard
    start, end = resolve_range(start_date, end_date, ctx.timezone)

    if granularity == "intraday":
        if not spec.supports_intraday:
            supported = sorted(n for n, m in METRICS.items() if m.supports_intraday)
            return ToolResult.error(
                f"{metric} does not support intraday granularity. Intraday is "
                f"available for: {', '.join(supported)}."
            )
        truncated = (end - start).days + 1 > MAX_INTRADAY_DAYS
        if truncated:
            start = end - timedelta(days=MAX_INTRADAY_DAYS - 1)
        return _intraday_series(ctx, spec, start, end, truncated)

    span = (end - start).days + 1
    if span > spec.max_range_days:
        return ToolResult.error(
            f"Range of {span} days exceeds the {spec.max_range_days}-day maximum "
            f"for {metric}. Request a narrower range."
        )

    series = fetch_metric(ctx.client, metric, start, end, ctx.timezone)

    if series.state is ResultState.ERROR:
        return ToolResult.error(series.message or "Failed to fetch metric.")
    if series.state is ResultState.WARMING_UP:
        return ToolResult.warming_up(series.message, warmup_nights=spec.warmup_nights)
    if series.state is ResultState.NO_DATA:
        return ToolResult.no_data(series.message)

    baseline = compute_baseline(list(series.by_day.values()))
    points = [
        {"date": day.isoformat(), "value": value}
        for day, value in sorted(series.by_day.items())
    ]

    return ToolResult.ok(
        {
            "metric": metric,
            "unit": spec.unit,
            "granularity": "daily",
            "range": {"start": start.isoformat(), "end": end.isoformat()},
            "baseline": baseline.to_dict(),
            "points": points,
        }
    )
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
pytest tests/test_metric_series.py -v
```

Expected: PASS — 6 passed

- [ ] **Step 5: Commit**

```bash
git add src/mcp_fitbit_air/server.py tests/test_metric_series.py
git commit -m "feat: add get_metric_series tool with intraday support and 7-day cap"
```

---

### Task 12: `get_sleep_detail` and `query_raw` tools

The two remaining tools, built together: both are small, and neither depends on the other.

**Files:**
- Modify: `src/mcp_fitbit_air/server.py` (append both tools)
- Create: `tests/test_sleep_detail.py`
- Create: `tests/test_query_raw.py`

**Interfaces:**
- Consumes: `HealthClient` (Task 6), `resolve_day` (Task 5), `ToolResult` (Task 5)
- Produces:
  - `server.get_sleep_detail(date: str)` tool
  - `server.query_raw(data_type: str, start_date: str, end_date: str | None = None, method: str = "list")` tool

Note: `get_sleep_detail`'s parameter is named `date` because that is the clearest
name for the tool's caller. Task 11 already imported `datetime as dt` rather than
`from datetime import date` so that nothing is shadowed here.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_sleep_detail.py`:

```python
from unittest.mock import Mock
from zoneinfo import ZoneInfo

import pytest


@pytest.fixture
def fake_context(monkeypatch):
    ctx = Mock()
    ctx.timezone = ZoneInfo("UTC")
    monkeypatch.setattr("mcp_fitbit_air.server.get_context", lambda: ctx)
    return ctx


def test_sleep_detail_returns_stage_segments(fake_context):
    from mcp_fitbit_air.server import get_sleep_detail

    fake_context.client.list_data_points.return_value = [
        {
            "sleep": {
                "durationSeconds": 27000,
                "levels": [
                    {"level": "deep", "startTime": "2026-08-01T00:10:00Z", "durationSeconds": 3600},
                    {"level": "rem", "startTime": "2026-08-01T01:10:00Z", "durationSeconds": 2400},
                ],
            },
            "interval": {"civilEndTime": "2026-08-01T07:00:00"},
        }
    ]

    result = get_sleep_detail("2026-08-01")

    assert result["state"] == "ok"
    assert len(result["data"]["sessions"]) == 1
    assert result["data"]["sessions"][0]["levels"][0]["level"] == "deep"


def test_no_sleep_recorded_is_no_data(fake_context):
    from mcp_fitbit_air.server import get_sleep_detail

    fake_context.client.list_data_points.return_value = []

    result = get_sleep_detail("2026-08-01")

    assert result["state"] == "no_data"
    assert "2026-08-01" in result["message"]


def test_relative_date_is_accepted(fake_context):
    from mcp_fitbit_air.server import get_sleep_detail

    fake_context.client.list_data_points.return_value = []

    result = get_sleep_detail("yesterday")

    assert result["state"] == "no_data"


def test_unparseable_date_is_an_error(fake_context):
    from mcp_fitbit_air.server import get_sleep_detail

    result = get_sleep_detail("whenever")

    assert result["state"] == "error"
    assert "yesterday" in result["message"]
```

Create `tests/test_query_raw.py`:

```python
from unittest.mock import Mock
from zoneinfo import ZoneInfo

import pytest


@pytest.fixture
def fake_context(monkeypatch):
    ctx = Mock()
    ctx.timezone = ZoneInfo("UTC")
    monkeypatch.setattr("mcp_fitbit_air.server.get_context", lambda: ctx)
    return ctx


def test_query_raw_list_passes_data_type_through(fake_context):
    from mcp_fitbit_air.server import query_raw

    fake_context.client.list_data_points.return_value = [{"floors": {"count": 12}}]

    result = query_raw("floors", "2026-08-01", "2026-08-02")

    assert result["state"] == "ok"
    assert result["data"]["dataPoints"][0]["floors"]["count"] == 12


def test_query_raw_daily_rollup_uses_rollup_call(fake_context):
    from mcp_fitbit_air.server import query_raw

    fake_context.client.daily_rollup.return_value = [{"distance": {"meters": 5000}}]

    result = query_raw("distance", "2026-08-01", "2026-08-02", method="dailyRollUp")

    fake_context.client.daily_rollup.assert_called_once()
    assert result["data"]["dataPoints"][0]["distance"]["meters"] == 5000


def test_unknown_method_is_rejected(fake_context):
    from mcp_fitbit_air.server import query_raw

    result = query_raw("floors", "2026-08-01", method="magic")

    assert result["state"] == "error"
    assert "list" in result["message"]


def test_empty_result_is_no_data(fake_context):
    from mcp_fitbit_air.server import query_raw

    fake_context.client.list_data_points.return_value = []

    result = query_raw("floors", "2026-08-01", "2026-08-02")

    assert result["state"] == "no_data"


def test_result_is_truncated_to_protect_the_context_window(fake_context):
    from mcp_fitbit_air.server import MAX_RAW_POINTS, query_raw

    fake_context.client.list_data_points.return_value = [
        {"floors": {"count": n}} for n in range(MAX_RAW_POINTS + 50)
    ]

    result = query_raw("floors", "2026-08-01", "2026-08-02")

    assert len(result["data"]["dataPoints"]) == MAX_RAW_POINTS
    assert result["truncated"] is True
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
pytest tests/test_sleep_detail.py tests/test_query_raw.py -v
```

Expected: FAIL — `ImportError: cannot import name 'get_sleep_detail'`

- [ ] **Step 3: Append both tools to `server.py`**

Add to the existing imports:

```python
from .dates import resolve_day
from .mapping import coerce_number
```

Then append:

```python
MAX_RAW_POINTS = 500


@mcp.tool()
@tool_guard
def get_sleep_detail(date: str) -> ToolResult:
    """Get the full sleep-stage breakdown for a single night.

    Returns each sleep session's stage segments (deep, light, REM, awake) with
    their start times and durations — more detail than get_daily_summary, which
    reports only total duration.

    `date` is the date the sleep ENDED (the morning you woke up), and accepts
    natural language: "yesterday", "2026-08-01".
    """
    ctx = get_context()
    day = resolve_day(date, ctx.timezone)
    metric = get_metric("sleep_duration")
    points = ctx.client.list_data_points(
        metric.data_type,
        filter_expr=build_filter(metric, day, day, ctx.timezone),
    )

    sessions = []
    for point in points:
        payload = point.get("sleep") or {}
        summary = payload.get("summary") or {}
        sessions.append(
            {
                "minutes_asleep": coerce_number(summary.get("minutesAsleep")),
                "minutes_awake": coerce_number(summary.get("minutesAwake")),
                "minutes_to_fall_asleep": coerce_number(summary.get("minutesToFallAsleep")),
                "stages_summary": summary.get("stagesSummary", []),
                "stages": payload.get("stages", []),
                "is_main_sleep": (payload.get("metadata") or {}).get("mainSleep"),
                "interval": payload.get("interval"),
            }
        )

    if not sessions:
        return ToolResult.no_data(
            f"No sleep recorded for {day.isoformat()}. The band may not have been "
            "worn overnight, or may not have synced."
        )

    return ToolResult.ok({"date": day.isoformat(), "sessions": sessions})


@mcp.tool()
@tool_guard
def query_raw(
    data_type: str,
    start_date: str,
    end_date: str | None = None,
    method: str = "list",
) -> ToolResult:
    """Escape hatch: query any Google Health API data type directly.

    Use this only when no other tool covers what is needed — the other tools
    return cleaner, better-labelled data.

    `data_type` is a Google Health API identifier, for example: floors,
    distance, total-calories, exercise, active-minutes, daily-vo2-max,
    daily-respiratory-rate, sedentary-period, weight, altitude.

    `method` is "list" (default, works for every data type) or "dailyRollUp"
    (per-day aggregation, supported only by steps, distance, heart-rate,
    active-zone-minutes, total-calories and similar cumulative types).

    Results are truncated at 500 data points.
    """
    if method not in {"list", "dailyRollUp"}:
        return ToolResult.error(
            f"Unknown method {method!r}. Use \"list\" or \"dailyRollUp\"."
        )

    ctx = get_context()
    start, end = resolve_range(start_date, end_date, ctx.timezone)

    if method == "dailyRollUp":
        points = ctx.client.daily_rollup(data_type, start, end)
    else:
        field = f"{data_type.replace('-', '_')}.interval.civil_start_time"
        exclusive_end = end + timedelta(days=1)
        filter_expr = (
            f'{field} >= "{start.isoformat()}" AND '
            f'{field} < "{exclusive_end.isoformat()}"'
        )
        points = ctx.client.list_data_points(data_type, filter_expr=filter_expr)

    if not points:
        return ToolResult.no_data(
            f"No {data_type} data between {start.isoformat()} and {end.isoformat()}."
        )

    truncated = len(points) > MAX_RAW_POINTS
    meta = (
        {
            "truncated": True,
            "reason": f"Truncated to the first {MAX_RAW_POINTS} of {len(points)} points.",
        }
        if truncated
        else {}
    )

    return ToolResult.ok(
        {
            "data_type": data_type,
            "method": method,
            "range": {"start": start.isoformat(), "end": end.isoformat()},
            "dataPoints": points[:MAX_RAW_POINTS],
        },
        **meta,
    )
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
pytest tests/test_sleep_detail.py tests/test_query_raw.py -v
```

Expected: PASS — 9 passed

- [ ] **Step 5: Run the whole suite**

```bash
pytest tests/ -v
```

Expected: PASS — all tests

- [ ] **Step 6: Commit**

```bash
git add src/mcp_fitbit_air/server.py tests/test_sleep_detail.py tests/test_query_raw.py
git commit -m "feat: add get_sleep_detail and query_raw tools"
```

---

### Task 13: Smoke script, README, and Claude wiring

**Files:**
- Create: `scripts/smoke.py`
- Create: `README.md` (replace the placeholder)
- Create: `tests/test_tool_contract.py`

**Interfaces:**
- Consumes: every tool from Tasks 7, 10, 11, 12
- Produces: a runnable, documented, installable server

- [ ] **Step 1: Write the cross-tool contract test**

Create `tests/test_tool_contract.py`:

```python
"""Every tool must honour the four-state response contract, whatever happens."""

from unittest.mock import Mock
from zoneinfo import ZoneInfo

import pytest

from mcp_fitbit_air.auth import AuthError
from mcp_fitbit_air.client import ApiError

VALID_STATES = {"ok", "no_data", "warming_up", "error"}


@pytest.fixture
def fake_context(monkeypatch):
    ctx = Mock()
    ctx.timezone = ZoneInfo("UTC")
    monkeypatch.setattr("mcp_fitbit_air.server.get_context", lambda: ctx)
    return ctx


def all_tool_calls():
    from mcp_fitbit_air import server

    return [
        ("get_profile_and_devices", lambda: server.get_profile_and_devices()),
        ("get_daily_summary", lambda: server.get_daily_summary("last week")),
        ("get_metric_series", lambda: server.get_metric_series("hrv", "last week")),
        ("get_sleep_detail", lambda: server.get_sleep_detail("yesterday")),
        ("query_raw", lambda: server.query_raw("floors", "last week")),
    ]


@pytest.mark.parametrize("name,call", all_tool_calls())
def test_tool_returns_valid_state_on_empty_data(fake_context, name, call):
    fake_context.client.list_data_points.return_value = []
    fake_context.client.daily_rollup.return_value = []
    fake_context.client.get_profile.return_value = {}
    fake_context.client.get_settings.return_value = {}
    fake_context.client.get_paired_devices.return_value = []

    result = call()

    assert result["state"] in VALID_STATES, f"{name} returned {result}"


@pytest.mark.parametrize("name,call", all_tool_calls())
def test_tool_returns_error_state_on_api_failure(fake_context, name, call):
    boom = ApiError("upstream exploded", status=500)
    fake_context.client.list_data_points.side_effect = boom
    fake_context.client.daily_rollup.side_effect = boom
    fake_context.client.get_profile.side_effect = boom
    fake_context.client.get_settings.side_effect = boom
    fake_context.client.get_paired_devices.side_effect = boom

    result = call()

    assert result["state"] == "error", f"{name} returned {result}"


@pytest.mark.parametrize("name,call", all_tool_calls())
def test_tool_returns_auth_remedy_when_unauthenticated(monkeypatch, name, call):
    from mcp_fitbit_air import server

    class Unauthenticated:
        timezone = ZoneInfo("UTC")

        @property
        def client(self):
            raise AuthError("no credentials on disk")

    monkeypatch.setattr(server, "get_context", lambda: Unauthenticated())

    result = call()

    assert result["state"] == "error"
    assert "mcp-fitbit-air auth" in result["remedy"], f"{name} returned {result}"


@pytest.mark.parametrize("name,call", all_tool_calls())
def test_tool_never_raises(fake_context, name, call):
    fake_context.client.list_data_points.side_effect = RuntimeError("chaos")
    fake_context.client.daily_rollup.side_effect = RuntimeError("chaos")
    fake_context.client.get_profile.side_effect = RuntimeError("chaos")
    fake_context.client.get_settings.side_effect = RuntimeError("chaos")
    fake_context.client.get_paired_devices.side_effect = RuntimeError("chaos")

    result = call()  # must not raise

    assert result["state"] == "error"
```

- [ ] **Step 2: Run the contract tests**

```bash
pytest tests/test_tool_contract.py -v
```

Expected: PASS — 20 passed (5 tools x 4 scenarios). Fix any tool that fails; the contract is the requirement, not the test.

- [ ] **Step 3: Write the smoke script**

Create `scripts/smoke.py`:

```python
"""Manual smoke test against the LIVE API.

Not part of the test suite. Run by hand to confirm that reality still matches
the fixtures — the Google Health API is only months old and may still change.

    python scripts/smoke.py
"""

from __future__ import annotations

import json
import sys

from mcp_fitbit_air import server


def show(label: str, payload: dict) -> None:
    state = payload.get("state")
    marker = {"ok": "OK  ", "no_data": "NONE", "warming_up": "WARM", "error": "ERR "}
    print(f"[{marker.get(state, '????')}] {label}", file=sys.stderr)
    if state in {"no_data", "warming_up", "error"}:
        print(f"       {payload.get('message', '')}", file=sys.stderr)
        if payload.get("remedy"):
            print(f"       remedy: {payload['remedy']}", file=sys.stderr)
    else:
        preview = json.dumps(payload.get("data"), indent=2)[:400]
        print(f"       {preview}", file=sys.stderr)
    print(file=sys.stderr)


def main() -> int:
    show("get_profile_and_devices", server.get_profile_and_devices())
    show("get_daily_summary(last week)", server.get_daily_summary("last week"))
    show("get_metric_series(hrv, last week)", server.get_metric_series("hrv", "last week"))
    show(
        "get_metric_series(heart_rate, yesterday, intraday)",
        server.get_metric_series("heart_rate", "yesterday", granularity="intraday"),
    )
    show("get_sleep_detail(yesterday)", server.get_sleep_detail("yesterday"))
    show("query_raw(floors, last week)", server.query_raw("floors", "last week"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run the smoke script against live data**

```bash
export FITBIT_MCP_CLIENT_ID='<your client id>'
export FITBIT_MCP_CLIENT_SECRET='<your client secret>'
python scripts/smoke.py
```

Expected: every tool returns `OK`, `NONE`, or `WARM`. Any `ERR` other than a genuine rate limit is a bug — fix it before continuing. `WARM` on `hrv` or `skin_temperature_deviation` is expected on a young account and is a pass, not a failure.

- [ ] **Step 5: Write the README**

Replace `README.md` entirely:

````markdown
# mcp-fitbit-air

An MCP server that lets Claude answer questions about your Google Fitbit Air
health data, via the Google Health API.

Ask things like *"How did I sleep this week?"*, *"Is my resting heart rate
trending up?"*, or *"Was my HRV low after Tuesday?"*

## Tools

| Tool | What it does |
|---|---|
| `get_daily_summary` | Day-by-day table: sleep, resting HR, HRV, steps, active zone minutes, SpO2, skin temperature. Start here. |
| `get_metric_series` | One metric over time, daily or intraday (minute-level). |
| `get_sleep_detail` | Full sleep-stage breakdown for one night. |
| `get_profile_and_devices` | Profile plus device battery and last sync time. |
| `query_raw` | Escape hatch for any data type not covered above. |

Every response carries units and a trailing 30-day baseline with its sample
size. Days with no data are reported as `no_data`, and metrics Fitbit has not
computed yet as `warming_up` — never as zero.

## Requirements

- Python 3.11+
- A Fitbit device paired to a Google account
- A Google Cloud project with the Google Health API enabled

## Setup

### 1. Create a Google Cloud project

1. Create a project at [console.cloud.google.com](https://console.cloud.google.com/).
2. Enable the **Google Health API**.
3. Configure the OAuth consent screen (**External**), and add your own Google
   account as a **Test user**.
4. Create an **OAuth client ID** of type **Desktop app**. Note the client ID and
   secret.

> **Publishing status matters.** While the app is in *Testing*, Google expires
> refresh tokens after 7 days, so you will have to re-authenticate weekly.
> Google Health scopes are *Restricted*; for personal use there is an exception
> to the usual CASA security assessment. Set the publishing status to
> *In production* if you can.

### 2. Install

```bash
git clone <this repo>
cd mcp-fitbit-air
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
```

### 3. Authenticate

```bash
cp .env.example .env
# edit .env and fill in your client ID and secret
mcp-fitbit-air auth
```

`.env` is gitignored. Exported environment variables take precedence over it,
so the `env` block in your MCP client config always wins.

A browser opens for consent. The refresh token is written to
`~/.config/mcp-fitbit-air/token.json` with `0600` permissions. The server
refreshes it silently from then on — you should not need to run this again.

### 4. Register with Claude

Add to your MCP client configuration (for Claude Desktop, this is
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

Restart the client, then ask: *"Is my Fitbit synced?"*

## Configuration

Set these as environment variables or in a `.env` file at the project root.
Environment variables win where both are present.

| Variable | Required | Default |
|---|---|---|
| `FITBIT_MCP_CLIENT_ID` | yes | — |
| `FITBIT_MCP_CLIENT_SECRET` | yes | — |
| `FITBIT_MCP_TOKEN_PATH` | no | `~/.config/mcp-fitbit-air/token.json` |

## Troubleshooting

**Every tool says "Run `mcp-fitbit-air auth`."** Either you have never
authenticated, or the refresh token was revoked. If this recurs weekly, the app
is still in *Testing* publishing status.

**Tools return `no_data`.** Call `get_profile_and_devices` and check
`lastSyncTime`. A stale sync explains missing data more often than anything
else.

**HRV or skin temperature says `warming_up`.** These need several nights of
wear before Fitbit computes them. Expected on a new device.

## Development

```bash
pip install -e ".[dev]"
pytest                    # offline; runs against fixtures
python scripts/smoke.py   # manual, hits the live API
```

Tests never call the live API. Fixtures in `tests/fixtures/` are scrubbed real
responses; regenerate them with `scripts/capture_fixtures.py` and **review the
output before committing** — they originate in real health data.

## Design

See [`docs/superpowers/specs/2026-08-02-fitbit-air-mcp-design.md`](docs/superpowers/specs/2026-08-02-fitbit-air-mcp-design.md).

## Limitations

- Read-only. No logging of workouts, weight, or food.
- No local storage: every call hits the API live.
- Single user.
- The legacy Fitbit Web API is deprecated 2026-09-30; this server does not use it.
````

- [ ] **Step 6: Verify a clean install works end to end**

```bash
deactivate 2>/dev/null; rm -rf /tmp/mcp-fitbit-air-check
python3 -m venv /tmp/mcp-fitbit-air-check
/tmp/mcp-fitbit-air-check/bin/pip install -e ".[dev]"
/tmp/mcp-fitbit-air-check/bin/pytest tests/ -v
```

Expected: install succeeds and the full suite passes in a fresh environment.

- [ ] **Step 7: Register with Claude and confirm the tools appear**

Add the config block from the README to your MCP client, restart it, and ask
*"Is my Fitbit synced?"* Expected: Claude calls `get_profile_and_devices` and
reports the battery level and last sync time.

- [ ] **Step 8: Commit**

```bash
git add README.md scripts/smoke.py tests/test_tool_contract.py
git commit -m "feat: add smoke script, cross-tool contract tests, and setup documentation"
```

---

## Self-Review

Checked after writing, against `docs/superpowers/specs/2026-08-02-fitbit-air-mcp-design.md`.

**Spec coverage:**

| Spec requirement | Task |
|---|---|
| Google Health API v4, not legacy | Tasks 1, 6 |
| Single-user, config-driven for open-sourcing | Task 2 (env-only config), Task 13 (README) |
| No local storage; `client.py` as the caching seam | Task 6 |
| Python, MCP SDK, stdio | Tasks 2, 7 |
| Split-out `auth` CLI; server refreshes silently only | Task 3 |
| Token at 0600, gitignored | Tasks 1, 3 |
| Phase 0 spike gates everything; profile/devices first | Task 1 |
| `get_daily_summary`, 90-day cap, concurrent mixed fan-out | Tasks 9, 10 |
| `get_metric_series`, 7-day intraday cap with truncation notice | Task 11 |
| `get_sleep_detail` | Task 12 |
| `get_profile_and_devices` as smoke test | Task 7 |
| `query_raw` with vocabulary in its description | Task 12 |
| Natural-language dates in profile timezone | Task 5 (`dates.py`), Task 7 (`context.timezone`) |
| Baselines with sample size | Tasks 8, 10, 11 |
| Units labelled everywhere | Task 4 (registry), Tasks 10, 11 |
| Four-state contract, `no_data`/`warming_up` not errors | Tasks 5, 9, 13 |
| Partial results; one metric failing does not sink others | Task 10 |
| Auth errors actionable; revoked vs never-authenticated | Task 3 |
| 429 backoff then honest message | Task 6 |
| Fixtures scrubbed, committed, no live calls in suite | Task 6 |
| Unit tests on dates, mapping, baselines, conversions | Tasks 4, 5, 8 |
| Contract tests for all four states | Task 13 |
| Manual smoke script | Task 13 |
| Success criterion: setup by a second person | Task 13 (README) |

No gaps found.

**Placeholder scan:** No TBD/TODO markers. Every code step contains complete
runnable code. The one deliberate "fill this in" is the Phase 0 findings
template in Task 1 — that is the task's deliverable, produced by observing the
live API, not a deferred decision.

**Type consistency:** Verified across tasks — `Config.from_env`, `AuthError.remedy`,
`ApiError.status`/`.remedy`, `Metric` field names (`value_key`, `filter_field`,
`warmup_nights`, `max_range_days`, `supports_intraday`), `MetricSeries.by_day`/`.state`/`.message`,
`ToolResult.ok`/`.no_data`/`.warming_up`/`.error` and `.to_dict()`, `Baseline.to_dict()`,
`HealthClient.list_data_points`/`.daily_rollup`/`.get_profile`/`.get_paired_devices`,
`resolve_range`/`resolve_day`. Names used in later tasks match their definitions.

**Known reconciliation points.** Three constants are asserted from documentation
but must be confirmed against Phase 0 output, with explicit reconciliation steps
already in the plan: the `dailyRollUp` range shape (Task 6, Step 8), the per-type
`filter_field` and `value_key` values (Task 4, Step 5), and the exact scope strings
(Task 1, Step 7 → `config.py`).
