"""Entry point for `python -m mcp_fitbit_air`.

The console script installed by pyproject is the documented way in, but wiring
an MCP client by hand invites `python -m <package>`, so it has to work too.
"""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
