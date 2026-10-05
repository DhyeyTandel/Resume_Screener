import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# The test suite must never pick up a developer's real .env: with a real key and
# LLM_PROVIDER=anthropic there, tests using the default client would make paid live API
# calls. Pin mock and drop real provider keys for the whole session. Individual tests that
# exercise providers set fake keys through monkeypatch.
import os

os.environ["SCREENING_NO_DOTENV"] = "1"
os.environ["LLM_PROVIDER"] = "mock"
for _k in ("ANTHROPIC_API_KEY", "GITHUB_TOKEN"):
    os.environ.pop(_k, None)

# The app-level rate limiter would 429 a suite that posts many screenings from one test
# client. Its behaviour is covered in isolation by test_limits.py with an injected clock.
from app import config as _config  # noqa: E402

_config.get_config()["limits"]["rate"]["enabled"] = False


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_screening_queue():
    """Some tests replace routes._run with fakes that never release their admission;
    reset the process-wide queue so one test's leftovers cannot fill it for the next."""
    from app.api.limits import screening_slots

    screening_slots.reset()
    yield
    screening_slots.reset()
