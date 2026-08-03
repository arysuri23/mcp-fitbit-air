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

from dotenv import load_dotenv
from google.auth.transport.requests import AuthorizedSession
from google_auth_oauthlib.flow import InstalledAppFlow

# Load .env from the repo root. Real environment variables win over the file.
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

BASE = "https://health.googleapis.com/v4"
RAW = Path(__file__).parent / "raw"

# Candidate scopes. If any is rejected, record the error verbatim in findings.
SCOPES = [
    "https://www.googleapis.com/auth/googlehealth.activity_and_fitness.readonly",
    "https://www.googleapis.com/auth/googlehealth.health_metrics_and_measurements.readonly",
    "https://www.googleapis.com/auth/googlehealth.sleep.readonly",
]


def authenticate() -> AuthorizedSession:
    client_id = os.environ.get("FITBIT_MCP_CLIENT_ID")
    client_secret = os.environ.get("FITBIT_MCP_CLIENT_SECRET")
    if not client_id or not client_secret:
        sys.exit(
            "FITBIT_MCP_CLIENT_ID and FITBIT_MCP_CLIENT_SECRET must be set.\n"
            "Copy .env.example to .env and fill in your OAuth client values."
        )
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
    RAW.mkdir(exist_ok=True)
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
