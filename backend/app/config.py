"""Config loader. Every threshold lives in config.yaml; nothing hard-coded."""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

_PATH = Path(__file__).with_name("config.yaml")


def _mini_yaml(text: str) -> dict:
    """Tiny YAML subset parser (nested maps + inline {a: 1, b: x} + lists).

    Assumption A-2: avoids a PyYAML dependency so the demo runs on a bare
    stdlib + FastAPI install. config.yaml must stay within this subset.
    """
    root: dict[str, Any] = {}
    stack: list[tuple[int, dict]] = [(-1, root)]

    def coerce(v: str) -> Any:
        v = v.strip()
        if v.startswith("[") and v.endswith("]"):
            return [coerce(p) for p in v[1:-1].split(",") if p.strip()]
        if v.startswith("{") and v.endswith("}"):
            out: dict[str, Any] = {}
            depth, cur = 0, ""
            for ch in v[1:-1]:
                if ch in "{[":
                    depth += 1
                if ch in "}]":
                    depth -= 1
                if ch == "," and depth == 0:
                    if cur.strip():
                        k, _, val = cur.partition(":")
                        out[k.strip()] = coerce(val)
                    cur = ""
                else:
                    cur += ch
            if cur.strip():
                k, _, val = cur.partition(":")
                out[k.strip()] = coerce(val)
            return out
        if v in ("true", "false"):
            return v == "true"
        if v in ("null", "~", ""):
            return None
        try:
            return int(v)
        except ValueError:
            pass
        try:
            return float(v)
        except ValueError:
            pass
        return v.strip('"').strip("'")

    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip())
        line = raw.strip().split(" #")[0].rstrip()
        key, _, val = line.partition(":")
        while stack and indent <= stack[-1][0]:
            stack.pop()
        parent = stack[-1][1]
        if val.strip() == "":
            node: dict[str, Any] = {}
            parent[key.strip()] = node
            stack.append((indent, node))
        else:
            parent[key.strip()] = coerce(val)
    return root


_ENV_FILE = Path(__file__).resolve().parents[2] / ".env"


def load_dotenv(path: Path = _ENV_FILE) -> list[str]:
    """Load KEY=VALUE lines from the repo-root .env (gitignored) into os.environ.

    Stdlib only. A variable already set in the real environment always wins. Values are
    never logged or returned; only the names that were set are returned, for diagnostics."""
    if os.getenv("SCREENING_NO_DOTENV") or not path.is_file():
        return []
    loaded = []
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.removeprefix("export ").partition("=")
        key, val = key.strip(), val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "'\"":
            val = val[1:-1]
        elif " #" in val:
            val = val.split(" #", 1)[0].rstrip()
        if key and val and key not in os.environ:
            os.environ[key] = val
            loaded.append(key)
    return loaded


@lru_cache(maxsize=1)
def get_config() -> dict:
    load_dotenv()
    cfg = _mini_yaml(_PATH.read_text())
    return _apply_env_overrides(cfg)


def _apply_env_overrides(cfg: dict) -> dict:
    """Env overrides for the documented switches (testable without touching the cache)."""
    if os.getenv("LLM_PROVIDER"):
        cfg["llm"]["provider"] = os.environ["LLM_PROVIDER"]
    # Documented in README, .env.example and docker-compose.yml but never read until an
    # audit caught it: under `docker compose --profile ollama` the app kept calling
    # localhost:11434, where Ollama is not running inside the app container.
    if os.getenv("OLLAMA_BASE_URL"):
        cfg["llm"]["ollama"]["base_url"] = os.environ["OLLAMA_BASE_URL"]
    if os.getenv("OLLAMA_MODEL"):
        cfg["llm"]["ollama"]["model"] = os.environ["OLLAMA_MODEL"]
    return cfg


def cfg(path: str, default: Any = None) -> Any:
    node: Any = get_config()
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node
