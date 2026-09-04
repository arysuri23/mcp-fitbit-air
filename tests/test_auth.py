import json
import stat
from pathlib import Path

import pytest

from mcp_fitbit_air.auth import AuthError, load_credentials, save_credentials
from mcp_fitbit_air.config import Config


def make_config(tmp_path: Path) -> Config:
    return Config(
        client_id="abc123",
        client_secret="shh",
        token_path=tmp_path / "token.json",
    )


def test_missing_token_file_raises_with_actionable_remedy(tmp_path):
    cfg = make_config(tmp_path)

    with pytest.raises(AuthError) as exc:
        load_credentials(cfg)

    assert "mcp-fitbit-air auth" in exc.value.remedy


def test_corrupt_token_file_raises_auth_error(tmp_path):
    cfg = make_config(tmp_path)
    cfg.token_path.parent.mkdir(parents=True, exist_ok=True)
    cfg.token_path.write_text("this is not json")

    with pytest.raises(AuthError) as exc:
        load_credentials(cfg)

    assert "mcp-fitbit-air auth" in exc.value.remedy


def test_token_without_refresh_token_is_rejected(tmp_path):
    """A token with no refresh_token cannot be silently renewed, so it is
    useless to the server even if it currently has a valid access token."""
    cfg = make_config(tmp_path)
    cfg.token_path.parent.mkdir(parents=True, exist_ok=True)
    cfg.token_path.write_text(json.dumps({"token": "abc", "scopes": []}))

    with pytest.raises(AuthError) as exc:
        load_credentials(cfg)

    assert "refresh" in str(exc.value).lower()


def test_save_credentials_writes_owner_only_permissions(tmp_path):
    class FakeCreds:
        def to_json(self):
            return json.dumps({"token": "abc", "refresh_token": "r"})

    token_path = tmp_path / "nested" / "token.json"
    save_credentials(FakeCreds(), token_path)

    assert token_path.exists()
    mode = stat.S_IMODE(token_path.stat().st_mode)
    assert mode == 0o600, f"expected 0600, got {oct(mode)}"


def test_saved_token_round_trips(tmp_path):
    class FakeCreds:
        def to_json(self):
            return json.dumps({"token": "abc", "refresh_token": "r"})

    token_path = tmp_path / "token.json"
    save_credentials(FakeCreds(), token_path)

    assert json.loads(token_path.read_text())["refresh_token"] == "r"


def test_refresh_error_on_expired_token_with_testing_remedy(tmp_path, monkeypatch):
    """RefreshError when refresh_token exists but refresh fails (e.g., 7-day expiry).

    In 'Testing' publishing status, Google expires refresh tokens after 7 days.
    This test simulates that scenario and verifies the error message guides the user.
    """
    from datetime import datetime, timedelta

    from google.auth.exceptions import RefreshError
    from google.oauth2.credentials import Credentials

    cfg = make_config(tmp_path)
    cfg.token_path.parent.mkdir(parents=True, exist_ok=True)

    # Create a token with refresh_token but an expired access token
    # (so creds.valid is False and refresh is actually attempted).
    # expiry must be in the past to make valid == False.
    past_time = (datetime.utcnow() - timedelta(hours=1)).isoformat() + "Z"
    token_data = {
        "token": "expired_access_token",
        "refresh_token": "valid_refresh_token",
        "expiry": past_time,
        "scopes": [],
    }
    cfg.token_path.write_text(json.dumps(token_data))

    # Patch Credentials.refresh to raise RefreshError (simulating 7-day expiry)
    def mock_refresh(self, request):
        raise RefreshError("Token has been revoked")

    monkeypatch.setattr(Credentials, "refresh", mock_refresh)

    with pytest.raises(AuthError) as exc:
        load_credentials(cfg)

    error_msg = str(exc.value)
    assert "Testing" in error_msg, f"Expected 'Testing' in error message: {error_msg}"
    assert "7 days" in error_msg or "7-day" in error_msg, (
        f"Expected '7 days' or '7-day' in error message: {error_msg}"
    )
    assert "mcp-fitbit-air auth" in exc.value.remedy
