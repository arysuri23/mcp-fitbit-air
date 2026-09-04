"""Manual smoke test against the LIVE API.

Not part of the test suite, and never run by pytest. Run it by hand to confirm
reality still matches the fixtures: the Google Health API is months old and has
already changed shape once during this project.

    python scripts/smoke.py
    python scripts/smoke.py --skip-intraday   # skip the slow one

Exit code 0 means every tool answered. Exit 1 means at least one returned an
error, which is a bug or an expired token - not something to shrug at.

Everything prints to stderr, matching the rule the server itself lives under:
stdout belongs to the MCP transport and nothing else.
"""

from __future__ import annotations

import json
import sys
import time
from typing import Callable

from mcp_fitbit_air import server

MARKERS = {"ok": "OK  ", "no_data": "NONE", "warming_up": "WARM", "error": "ERR "}


def show(label: str, call: Callable[[], dict]) -> str:
    started = time.monotonic()
    payload = call()
    elapsed = time.monotonic() - started

    state = payload.get("state", "")
    print(f"[{MARKERS.get(state, '????')}] {label}  ({elapsed:.1f}s)", file=sys.stderr)

    if state in {"no_data", "warming_up", "error"}:
        print(f"       {payload.get('message', '')}", file=sys.stderr)
        if payload.get("remedy"):
            print(f"       remedy: {payload['remedy']}", file=sys.stderr)
    else:
        preview = json.dumps(payload.get("data"), indent=2)[:400]
        print(f"       {preview}", file=sys.stderr)

    if payload.get("truncated"):
        print(f"       truncated: {payload.get('reason', '')}", file=sys.stderr)

    print(file=sys.stderr)
    return state


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv

    checks: list[tuple[str, Callable[[], dict]]] = [
        ("get_profile_and_devices", server.get_profile_and_devices),
        ("get_daily_summary(last week)", lambda: server.get_daily_summary("last week")),
        (
            "get_metric_series(hrv, last week)",
            lambda: server.get_metric_series("hrv", "last week"),
        ),
        ("get_sleep_detail(yesterday)", lambda: server.get_sleep_detail("yesterday")),
        # floors rejects "list" with a 400 naming the methods it does support;
        # verified live. The plan's original example used list here, which would
        # have made this script report a failure on every single run.
        (
            "query_raw(floors, last week, dailyRollUp)",
            lambda: server.query_raw("floors", "last week", method="dailyRollUp"),
        ),
    ]

    if "--skip-intraday" not in args:
        # Slowest by far: the band samples heart rate every couple of seconds,
        # and the API throttles hard after a large pull, so this one can sit for
        # minutes without being broken.
        checks.insert(
            4,
            (
                "get_metric_series(heart_rate, yesterday, intraday)",
                lambda: server.get_metric_series(
                    "heart_rate", "yesterday", granularity="intraday"
                ),
            ),
        )

    states = [show(label, call) for label, call in checks]

    failed = sum(1 for state in states if state == "error")
    print(
        f"{len(states) - failed}/{len(states)} tools answered without error.",
        file=sys.stderr,
    )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
