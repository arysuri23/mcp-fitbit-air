"""Convert raw spike output into scrubbed test fixtures.

Fixtures exist to pin the API's SHAPE: key names, nesting, which integers arrive
as JSON strings, and sentinel values like the literal "NaN" the API returns for
metrics it cannot compute yet. None of that requires real health data.

So this script preserves structure and sentinels exactly, and replaces the
personal parts:

- identifiers (resource paths, MAC addresses, device/user ids) become placeholders
- every real timestamp and civil date is shifted onto a fixed synthetic anchor
- numeric health readings are replaced with plausible synthetic values, keeping
  their JSON type — a value that arrived as the string "8630" leaves as a string

Run from the repo root:

    .venv/bin/python scripts/capture_fixtures.py
"""

from __future__ import annotations

import json
import re
import sys
from datetime import date, timedelta
from pathlib import Path

RAW_DIRS = [Path("spike/raw"), Path("spike/raw2")]
OUT = Path("tests/fixtures")

# All real dates are shifted so that the most recent becomes this date.
ANCHOR = date(2026, 1, 8)

# Keys whose values are identifiers rather than data.
ID_KEYS = {
    "userId",
    "user",
    "name",
    "deviceId",
    "serialNumber",
    "macAddress",
    "pageToken",
    "nextPageToken",
}

# Matches a bare date, and a date at the start of an RFC-3339 timestamp.
# An earlier version used \b at both ends, which silently failed to match inside
# "2026-08-02T14:50:00Z" because "2" and "T" are both word characters — so no
# timestamp was ever shifted.
DATE_RE = re.compile(r"(?<!\d)(\d{4})-(\d{2})-(\d{2})(?!\d)")

# Health readings, keyed by the field name they arrive under. Values are chosen
# to be plausible and stable, never derived from the real ones.
SYNTHETIC: dict[str, float] = {
    "beatsPerMinute": 60,
    "averageHeartRateVariabilityMilliseconds": 45.0,
    "nonRemHeartRateBeatsPerMinute": 59,
    "entropy": 1.5,
    "deepSleepRootMeanSquareOfSuccessiveDifferencesMilliseconds": 40.0,
    "averagePercentage": 96.0,
    "lowerBoundPercentage": 94.0,
    "upperBoundPercentage": 98.0,
    "nightlyTemperatureCelsius": 34.0,
    "baselineTemperatureCelsius": 33.8,
    "relativeNightlyStddev30dCelsius": 0.4,
    "count": 100,
    "countSum": 9000,
    "activeZoneMinutes": 1,
    "sumInFatBurnHeartZone": 5,
    "sumInCardioHeartZone": 2,
    "sumInPeakHeartZone": 0,
    "minutesInSleepPeriod": 400,
    "minutesAfterWakeUp": 0,
    "minutesToFallAsleep": 5,
    "minutesAsleep": 390,
    "minutesAwake": 10,
    "minutes": 60,
    "age": 30,
    "userConfiguredWalkingStrideLengthMm": 700,
    "userConfiguredRunningStrideLengthMm": 900,
    "batteryLevel": 80,
}

# Sentinels that carry meaning and must survive verbatim. The "NaN" string in
# particular is a real API value that caused a live bug.
PRESERVE_VALUES = {"NaN", "Infinity", "-Infinity"}

CIVIL_KEYS = {"year", "month", "day"}

# (parent, key) pairs that SYNTHETIC must not touch, because the same field name
# means something different here. `minutes` is a sleep-stage duration almost
# everywhere, but inside a CivilTime it is the minute of the hour - and
# synthesising 60 there produced fixtures the real API could never emit, which
# is precisely the vacuous shape these fixtures exist to rule out.
NEVER_SYNTHESISED = {("time", "minutes"), ("time", "hours"), ("time", "seconds")}


def _iter_dates(text: str) -> list[date]:
    found = []
    for match in DATE_RE.finditer(text):
        try:
            found.append(date.fromisoformat(match.group(0)))
        except ValueError:
            continue
    return found


def collect_dates(node) -> list[date]:
    """Every date in the payload, whether in a string or a civil-date object."""
    found = _iter_dates(json.dumps(node))
    if isinstance(node, dict):
        if CIVIL_KEYS <= node.keys():
            try:
                found.append(date(int(node["year"]), int(node["month"]), int(node["day"])))
            except (TypeError, ValueError):
                pass
        for value in node.values():
            found.extend(collect_dates(value))
    elif isinstance(node, list):
        for value in node:
            found.extend(collect_dates(value))
    return found


def shift_text(text: str, shift: timedelta) -> str:
    def replace(match: re.Match) -> str:
        try:
            return (date.fromisoformat(match.group(0)) + shift).isoformat()
        except ValueError:
            return match.group(0)

    return DATE_RE.sub(replace, text)


def synth(key: str | None, value, parent: str | None = None):
    """Replace a health reading, preserving its JSON type."""
    if (parent, key) in NEVER_SYNTHESISED:
        return value
    if key not in SYNTHETIC:
        return value
    replacement = SYNTHETIC[key]
    if isinstance(value, str):
        if value in PRESERVE_VALUES:
            return value
        return str(int(replacement) if float(replacement).is_integer() else replacement)
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return int(replacement)
    if isinstance(value, float):
        return float(replacement)
    return value


def scrub(node, shift: timedelta, key: str | None = None, parent: str | None = None):
    if isinstance(node, dict):
        if CIVIL_KEYS <= node.keys():
            try:
                moved = date(int(node["year"]), int(node["month"]), int(node["day"])) + shift
                return {
                    **{
                        k: scrub(v, shift, k, key)
                        for k, v in node.items()
                        if k not in CIVIL_KEYS
                    },
                    "year": moved.year,
                    "month": moved.month,
                    "day": moved.day,
                }
            except (TypeError, ValueError):
                pass
        return {
            k: (
                f"redacted-{k}"
                if k in ID_KEYS and isinstance(v, str)
                else scrub(v, shift, k, key)
            )
            for k, v in node.items()
        }
    if isinstance(node, list):
        return [scrub(item, shift, key, parent) for item in node]
    if isinstance(node, str):
        return synth(key, node, parent) if key in SYNTHETIC else shift_text(node, shift)
    if isinstance(node, (int, float)) and not isinstance(node, bool):
        return synth(key, node, parent)
    return node


def main() -> int:
    sources = [(d, p) for d in RAW_DIRS if d.exists() for p in sorted(d.glob("*.json"))]
    if not sources:
        print("No raw spike output found. Run the Phase 0 spike first.", file=sys.stderr)
        return 1

    OUT.mkdir(parents=True, exist_ok=True)

    all_dates: list[date] = []
    for _, path in sources:
        all_dates.extend(collect_dates(json.loads(path.read_text())))
    shift = ANCHOR - max(all_dates) if all_dates else timedelta(0)

    for _, path in sources:
        payload = json.loads(path.read_text())
        (OUT / path.name).write_text(
            json.dumps(scrub(payload, shift), indent=2, sort_keys=True) + "\n"
        )
        print(f"wrote {OUT / path.name}", file=sys.stderr)

    print(
        f"\nShifted all dates by {shift.days} days and replaced health readings with "
        "synthetic values.\nREVIEW THE OUTPUT BEFORE COMMITTING.",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
