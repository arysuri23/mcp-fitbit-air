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
