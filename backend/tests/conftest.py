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
