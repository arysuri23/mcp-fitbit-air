"""MCP server exposing Fitbit Air data.

Never write to stdout from this process: stdout is the MCP protocol stream.
Diagnostics go to stderr via logging.
"""

from __future__ import annotations

import functools
import inspect
import logging
import sys
from typing import Any, Callable

from mcp.server.mcpserver import MCPServer

from .auth import AuthError
from .client import ApiError
from .config import ConfigError
from .context import get_context
from .dates import DateParseError, resolve_range
from .mapping import SUMMARY_METRICS, UnknownMetricError
from .results import ToolResult
from .summary import build_summary

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

    # `functools.wraps` copies `fn`'s `__annotations__` (so `-> ToolResult`
    # silently overwrites `wrapper`'s own `-> dict[str, Any]`) AND sets
    # `wrapper.__wrapped__ = fn`. That second part is the one that actually
    # bites: the MCP SDK builds each tool's schema by calling
    # `inspect.signature(tool_fn, eval_str=True)`, and `inspect.signature`
    # follows `__wrapped__` by default whenever the object has no
    # `__signature__` of its own — so it skips straight past `wrapper` and
    # inspects `fn` instead, recovering `fn`'s original `-> ToolResult`
    # return annotation no matter what `wrapper.__annotations__` says.
    # (Confirmed directly: overwriting `wrapper.__annotations__` alone does
    # NOT change what `inspect.signature(wrapper)` reports, because it never
    # looks at `wrapper.__annotations__` once it has unwrapped past it.) The
    # SDK then builds the output schema from the `ToolResult` dataclass
    # (requires `state` AND a nested `meta` key) and validates `wrapper`'s
    # actual return value — a dict flattened by `ToolResult.to_dict()`, with
    # no `meta` key by design — against that schema. Every call to every
    # `tool_guard`-wrapped tool then fails SDK output validation, even though
    # the tool itself succeeded. This is invisible to tests that call the
    # wrapped function directly in Python, since that path never touches the
    # SDK's schema/signature machinery at all.
    #
    # Setting `__signature__` explicitly makes `wrapper` self-describing
    # again: `inspect.signature` stops unwrapping as soon as it finds an
    # object that already carries `__signature__`, so it uses this signature
    # instead of following `__wrapped__` to `fn`. Parameters are copied
    # unchanged from `fn` (via `eval_str=True`, to resolve the string
    # annotations `from __future__ import annotations` produces) since later
    # tools take real arguments (metric name, date range, ...) that must
    # still appear in the input schema — only the return annotation changes.
    original_signature = inspect.signature(fn, eval_str=True)
    wrapper.__signature__ = original_signature.replace(return_annotation=dict[str, Any])
    wrapper.__annotations__ = {
        **getattr(fn, "__annotations__", {}),
        "return": dict[str, Any],
    }

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

    Every value carries its unit and a trailing baseline computed over a window
    reaching up to 30 days before the requested range, reported alongside the
    sample size it came from — a baseline with a small n is not a settled norm.
    Days with no data are marked no_data rather than zero, and metrics Fitbit
    has not computed yet are marked warming_up.
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


def run_server() -> None:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    mcp.run()
