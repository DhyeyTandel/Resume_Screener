"""The repo-root .env (gitignored) is how API keys reach `make dev` without the shell."""
from app.config import load_dotenv


def test_dotenv_loads_without_overriding_and_never_returns_values(tmp_path, monkeypatch):
    monkeypatch.delenv("SCREENING_NO_DOTENV")  # conftest disables .env for the suite
    env = tmp_path / ".env"
    env.write_text(
        "# comment\n"
        "ANTHROPIC_API_KEY=sk-test-secret-123\n"
        'export GITHUB_TOKEN="ghp_quoted"\n'
        "LLM_PROVIDER=anthropic  # inline comment\n"
        "EMPTY=\n"
        "ALREADY_SET=from-file\n"
    )
    # setenv-then-delenv makes monkeypatch record each key's original state, so everything
    # load_dotenv writes into os.environ is undone after the test. Without this the fake
    # LLM_PROVIDER=anthropic leaked into later tests, which then made live HTTP calls.
    for k in ("ANTHROPIC_API_KEY", "GITHUB_TOKEN", "LLM_PROVIDER", "EMPTY"):
        monkeypatch.setenv(k, "placeholder")
        monkeypatch.delenv(k)
    monkeypatch.setenv("ALREADY_SET", "from-shell")

    loaded = load_dotenv(env)

    import os
    assert os.environ["ANTHROPIC_API_KEY"] == "sk-test-secret-123"
    assert os.environ["GITHUB_TOKEN"] == "ghp_quoted"
    assert os.environ["LLM_PROVIDER"] == "anthropic"
    assert "EMPTY" not in os.environ
    assert os.environ["ALREADY_SET"] == "from-shell"  # the real environment always wins
    assert set(loaded) == {"ANTHROPIC_API_KEY", "GITHUB_TOKEN", "LLM_PROVIDER"}
    assert "sk-test-secret-123" not in repr(loaded)  # names only, never values


def test_missing_dotenv_is_fine(tmp_path):
    assert load_dotenv(tmp_path / "nope.env") == []


def test_suite_never_loads_a_developers_real_dotenv(tmp_path):
    """conftest sets SCREENING_NO_DOTENV so a real key in .env can never reach tests."""
    env = tmp_path / ".env"
    env.write_text("LLM_PROVIDER=anthropic\n")
    assert load_dotenv(env) == []
