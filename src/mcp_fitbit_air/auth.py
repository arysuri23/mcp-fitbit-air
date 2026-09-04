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
    # rotating them does not require re-authentication. These are assignments
    # rather than setdefault on purpose: Credentials.to_json() always writes both
    # keys, so every token file this app has ever written already has them and a
    # setdefault could never take effect. The symptom was a rotated secret still
    # refreshing with the stale one and failing as invalid_client, reported as
    # "credentials were rejected" - which points at re-authenticating rather than
    # at the secret that actually changed.
    info["client_id"] = config.client_id
    info["client_secret"] = config.client_secret
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
