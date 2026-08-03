import json
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def load_fixture():
    """Return the `body` of a captured spike response."""

    def _load(name: str):
        path = FIXTURES / f"{name}.json"
        if not path.exists():
            pytest.skip(f"fixture {name}.json not captured")
        return json.loads(path.read_text())["body"]

    return _load


@pytest.fixture
def fake_credentials():
    """Credentials stand-in. AuthorizedSession is bypassed in client tests via
    the `session` injection point, so these need no real token machinery."""

    class FakeCreds:
        valid = True
        token = "fake-token"

        def refresh(self, request):  # pragma: no cover - never called when valid
            raise AssertionError("refresh should not be called on valid credentials")

    return FakeCreds()
