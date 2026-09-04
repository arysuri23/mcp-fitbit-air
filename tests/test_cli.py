"""The CLI as a user actually reaches it, via subprocess.

`python -m mcp_fitbit_air.cli serve` used to import the module and exit 0 with
no output and no server - the single most confusing way this can fail, because
it looks like success. Nothing but running it in a real process catches that:
importing cli and calling main() works fine either way.
"""

import subprocess
import sys

import pytest


def run_module(module: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", module, *args],
        capture_output=True,
        text=True,
        timeout=60,
    )


@pytest.mark.parametrize("module", ["mcp_fitbit_air", "mcp_fitbit_air.cli"])
def test_running_the_module_with_no_arguments_reports_usage(module):
    result = run_module(module)

    assert result.returncode == 2, result
    assert "usage" in result.stderr.lower()


@pytest.mark.parametrize("module", ["mcp_fitbit_air", "mcp_fitbit_air.cli"])
def test_running_the_module_offers_both_subcommands(module):
    result = run_module(module, "--help")

    assert result.returncode == 0, result
    assert "auth" in result.stdout
    assert "serve" in result.stdout


@pytest.mark.parametrize("module", ["mcp_fitbit_air", "mcp_fitbit_air.cli"])
def test_an_unknown_subcommand_is_rejected(module):
    result = run_module(module, "srve")

    assert result.returncode == 2, result
