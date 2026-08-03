import pytest

from mcp_fitbit_air.config import Config, ConfigError


def test_from_env_reads_required_values(monkeypatch, tmp_path):
    monkeypatch.setenv("FITBIT_MCP_CLIENT_ID", "abc123")
    monkeypatch.setenv("FITBIT_MCP_CLIENT_SECRET", "shh")
    monkeypatch.setenv("FITBIT_MCP_TOKEN_PATH", str(tmp_path / "token.json"))

    cfg = Config.from_env()

    assert cfg.client_id == "abc123"
    assert cfg.client_secret == "shh"
    assert cfg.token_path == tmp_path / "token.json"


def test_token_path_defaults_to_config_dir(monkeypatch):
    monkeypatch.setenv("FITBIT_MCP_CLIENT_ID", "abc123")
    monkeypatch.setenv("FITBIT_MCP_CLIENT_SECRET", "shh")
    monkeypatch.delenv("FITBIT_MCP_TOKEN_PATH", raising=False)

    cfg = Config.from_env()

    assert cfg.token_path.name == "token.json"
    assert "mcp-fitbit-air" in str(cfg.token_path)


def test_missing_client_id_raises_actionable_error(monkeypatch, tmp_path):
    monkeypatch.delenv("FITBIT_MCP_CLIENT_ID", raising=False)
    monkeypatch.setenv("FITBIT_MCP_CLIENT_SECRET", "shh")
    monkeypatch.chdir(tmp_path)  # no .env to fall back on

    with pytest.raises(ConfigError) as exc:
        Config.from_env()

    assert "FITBIT_MCP_CLIENT_ID" in str(exc.value)
    assert ".env" in str(exc.value)


def test_values_are_read_from_a_dotenv_file(monkeypatch, tmp_path):
    monkeypatch.delenv("FITBIT_MCP_CLIENT_ID", raising=False)
    monkeypatch.delenv("FITBIT_MCP_CLIENT_SECRET", raising=False)
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(
        "FITBIT_MCP_CLIENT_ID=from-dotenv\nFITBIT_MCP_CLIENT_SECRET=secret-from-dotenv\n"
    )

    cfg = Config.from_env()

    assert cfg.client_id == "from-dotenv"
    assert cfg.client_secret == "secret-from-dotenv"


def test_real_environment_wins_over_dotenv(monkeypatch, tmp_path):
    """Standard precedence: an explicitly exported variable beats the file, so
    Claude Desktop's `env` block overrides a stale .env left in the repo."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("FITBIT_MCP_CLIENT_ID=from-dotenv\n")
    monkeypatch.setenv("FITBIT_MCP_CLIENT_ID", "from-environment")
    monkeypatch.setenv("FITBIT_MCP_CLIENT_SECRET", "shh")

    cfg = Config.from_env()

    assert cfg.client_id == "from-environment"


def test_client_config_shape_matches_installed_app_flow(monkeypatch):
    monkeypatch.setenv("FITBIT_MCP_CLIENT_ID", "abc123")
    monkeypatch.setenv("FITBIT_MCP_CLIENT_SECRET", "shh")

    cfg = Config.from_env()

    assert cfg.client_config()["installed"]["client_id"] == "abc123"
    assert cfg.client_config()["installed"]["token_uri"].startswith("https://")
