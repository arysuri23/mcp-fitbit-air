"""Phase 0, round 2. Throwaway — not production code.

Round 1 established that auth works and data exists, and turned up four
problems. This round resolves all of them in one consent:

1. profile / pairedDevices returned 403 — missing profile & settings scopes.
2. The `daily-*` filter members were wrong: those data points carry a `date`
   object, not an `interval`. Tries several candidate member paths.
3. The dailyRollUp `range` shape was wrong in both candidates. `range.start`
   is a valid field but `year/month/day` are not its subfields, so it is the
   nested CivilDateTime form.
4. Captures real response shapes for the value paths the mapping table needs.

Saves the token to spike/token.json so later probing needs no re-consent.
"""

import json
import os
import sys
from datetime import date, timedelta
from pathlib import Path

from dotenv import load_dotenv
from google.auth.transport.requests import AuthorizedSession, Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

BASE = "https://health.googleapis.com/v4"
RAW = Path(__file__).parent / "raw2"
TOKEN = Path(__file__).parent / "token.json"  # gitignored via bare `token.json`

SCOPES = [
    "https://www.googleapis.com/auth/googlehealth.activity_and_fitness.readonly",
    "https://www.googleapis.com/auth/googlehealth.health_metrics_and_measurements.readonly",
    "https://www.googleapis.com/auth/googlehealth.sleep.readonly",
    # New in round 2 — round 1's profile/pairedDevices calls 403'd without these.
    "https://www.googleapis.com/auth/googlehealth.profile.readonly",
    "https://www.googleapis.com/auth/googlehealth.settings.readonly",
]


def session() -> AuthorizedSession:
    if TOKEN.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN), SCOPES)
        if creds.valid:
            return AuthorizedSession(creds)
        if creds.refresh_token:
            try:
                creds.refresh(Request())
                TOKEN.write_text(creds.to_json())
                return AuthorizedSession(creds)
            except Exception as exc:
                print(f"refresh failed ({exc}); re-consenting", file=sys.stderr)

    client_id = os.environ.get("FITBIT_MCP_CLIENT_ID")
    client_secret = os.environ.get("FITBIT_MCP_CLIENT_SECRET")
    if not client_id or not client_secret:
        sys.exit("Set FITBIT_MCP_CLIENT_ID and FITBIT_MCP_CLIENT_SECRET in .env")

    flow = InstalledAppFlow.from_client_config(
        {
            "installed": {
                "client_id": client_id,
                "client_secret": client_secret,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": ["http://localhost"],
            }
        },
        scopes=SCOPES,
    )
    creds = flow.run_local_server(port=0, access_type="offline", prompt="consent")
    TOKEN.write_text(creds.to_json())
    os.chmod(TOKEN, 0o600)
    print(f"token saved to {TOKEN}", file=sys.stderr)
    return AuthorizedSession(creds)


def save(name: str, status: int, body) -> None:
    RAW.mkdir(exist_ok=True)
    (RAW / f"{name}.json").write_text(json.dumps({"status": status, "body": body}, indent=2, default=str))


def brief(body) -> str:
    """One-line outcome for the console, without dumping health values."""
    if isinstance(body, dict):
        if "error" in body:
            return f"ERROR {body['error'].get('message','')[:110]}"
        for k in ("dataPoints", "rollupDataPoints", "pairedDevices", "devices"):
            if k in body:
                return f"OK {k}={len(body[k] or [])}"
        return f"OK keys={list(body.keys())[:6]}"
    return "OK"


def get(s, name, url, **params):
    try:
        r = s.get(url, params=params, timeout=30)
        b = r.json() if r.content else None
        save(name, r.status_code, b)
        print(f"  [{r.status_code}] {name}: {brief(b)}", file=sys.stderr)
        return r.status_code, b
    except Exception as exc:
        save(name, -1, {"exception": repr(exc)})
        print(f"  [xxx] {name}: {exc}", file=sys.stderr)
        return -1, None


def post(s, name, url, payload):
    try:
        r = s.post(url, json=payload, timeout=30)
        b = r.json() if r.content else None
        save(name, r.status_code, {"request": payload, "response": b})
        print(f"  [{r.status_code}] {name}: {brief(b)}", file=sys.stderr)
        return r.status_code, b
    except Exception as exc:
        save(name, -1, {"request": payload, "exception": repr(exc)})
        print(f"  [xxx] {name}: {exc}", file=sys.stderr)
        return -1, None


def main() -> None:
    RAW.mkdir(exist_ok=True)
    s = session()
    end = date.today()
    start = end - timedelta(days=7)

    print("\n--- 1. profile & devices (needs the new scopes) ---", file=sys.stderr)
    get(s, "profile", f"{BASE}/users/me/profile")
    get(s, "paired_devices", f"{BASE}/users/me/pairedDevices")
    get(s, "settings", f"{BASE}/users/me/settings")

    print("\n--- 2. filter member candidates for daily-* types ---", file=sys.stderr)
    # Round 1 proved these points carry `date`, not `interval`.
    for dt_name, member in [
        ("daily-resting-heart-rate", "daily_resting_heart_rate"),
        ("daily-heart-rate-variability", "daily_heart_rate_variability"),
        ("daily-oxygen-saturation", "daily_oxygen_saturation"),
        ("daily-sleep-temperature-derivations", "daily_sleep_temperature_derivations"),
    ]:
        for label, expr in [
            ("date", f'{member}.date >= "{start.isoformat()}" AND {member}.date < "{end.isoformat()}"'),
            ("civil_date", f'{member}.civil_date >= "{start.isoformat()}" AND {member}.civil_date < "{end.isoformat()}"'),
        ]:
            get(
                s,
                f"filter_{dt_name}__{label}",
                f"{BASE}/users/me/dataTypes/{dt_name}/dataPoints",
                filter=expr,
                pageSize=50,
            )

    print("\n--- 3. dailyRollUp range shape candidates ---", file=sys.stderr)
    civil = lambda d: {"date": {"year": d.year, "month": d.month, "day": d.day}}
    for dt_name in ["steps", "active-zone-minutes"]:
        url = f"{BASE}/users/me/dataTypes/{dt_name}/dataPoints:dailyRollUp"
        post(s, f"rollup_{dt_name}__nested_date", url,
             {"range": {"start": civil(start), "end": civil(end)}, "windowSizeDays": 1})
        post(s, f"rollup_{dt_name}__nested_datetime", url,
             {"range": {"start": {**civil(start), "time": {"hours": 0}},
                        "end": {**civil(end), "time": {"hours": 0}}},
              "windowSizeDays": 1})

    print("\n--- 4. list shapes for rollup-capable types ---", file=sys.stderr)
    get(s, "list_steps_nofilter", f"{BASE}/users/me/dataTypes/steps/dataPoints", pageSize=5)
    get(s, "list_azm_nofilter", f"{BASE}/users/me/dataTypes/active-zone-minutes/dataPoints", pageSize=5)
    get(s, "list_heart_rate_nofilter", f"{BASE}/users/me/dataTypes/heart-rate/dataPoints", pageSize=5)

    print(f"\nDone. Raw output in {RAW}", file=sys.stderr)


if __name__ == "__main__":
    main()
