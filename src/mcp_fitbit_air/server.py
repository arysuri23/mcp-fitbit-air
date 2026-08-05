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
