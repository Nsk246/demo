"""Settings, read once from the environment.

The env-file resolution and base-URL fallback are lifted from the restaurant
build. Both encode failures that were expensive to diagnose the first time.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


def _env_file() -> str:
    """Absolute path to the repo-root .env, if there is one.

    Not a relative ".env": that resolves against the working directory, so a
    repo-root file is silently ignored and every setting falls back to its
    default. Walk ancestors rather than indexing a fixed depth, because in the
    container the app sits at /srv/app and parents[3] raises IndexError at
    import, killing the process before it can serve anything.
    """
    here = Path(__file__).resolve()
    for base in here.parents:
        candidate = base / ".env"
        if candidate.is_file():
            return str(candidate)
    return ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=_env_file(), extra="ignore")

    app_env: str = "dev"
    public_base_url: str = ""

    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_validate_signature: bool = True
    twilio_number: str = ""

    realtime_provider: str = "gemini"
    gemini_api_key: str = ""
    gemini_live_model: str = "gemini-3.1-flash-live-preview"
    gemini_thinking_level: str = "minimal"
    gemini_end_of_speech_ms: int = 500
    gemini_voice: str = "Aoede"
    gemini_text_model: str = "gemini-2.5-flash"

    embedding_model: str = "gemini-embedding-001"
    embedding_dims: int = 768

    site_db: str = "data/site.db"
    # Answers the website does not contain. See app/facts.py.
    facts_file: str = "data/facts.md"
    site_timezone: str = "America/Chicago"

    # Retrieval. MIN_SCORE is the dial that decides whether the agent answers
    # off-topic questions with whatever chunk scored highest.
    top_k: int = 4
    min_score: float = 0.58
    # Off. A distribution-relative gate measured worse than the raw score on
    # real data, rejecting good questions more often than bad ones. Kept as a
    # diagnostic that eval prints. See the note in retrieval.Index.search.
    min_z: float = 0.0

    # A lookup is an embed call plus a matrix multiply, and the embed call is
    # the whole cost. Measured on a real call, 2500ms was not enough and every
    # lookup timed out: the client was rebuilt per request and the retry
    # backoff alone exceeded the budget. Both are fixed, but the ceiling stays
    # generous because a slow answer beats a wrong one.
    tool_timeout_ms: int = 8000
    max_call_seconds: int = 600

    max_pages: int = 120
    max_depth: int = 3
    crawl_timeout_s: float = 15.0
    crawl_concurrency: int = 6


def resolve_base_url(configured: str, env: dict[str, str]) -> str:
    """Normalise the public hostname, falling back to the platform's own.

    Split out from get_settings so it can be tested as a pure function.
    Testing it through Settings makes the result depend on whether the
    developer running the suite happens to have a .env, which is the kind of
    environment-dependent test that passes for me and fails for you.
    """
    url = configured
    if not url:
        for var in ("RAILWAY_PUBLIC_DOMAIN", "FLY_APP_NAME"):
            value = (env.get(var) or "").strip()
            if value:
                url = value if "." in value else f"{value}.fly.dev"
                break
    return url.replace("https://", "").replace("http://", "").strip("/")


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.public_base_url = resolve_base_url(
        settings.public_base_url, dict(os.environ)
    )
    return settings


def log_config_source() -> str:
    """Which .env was read, and whether it existed.

    An env file that is silently not found looks identical to one with every
    value left at its default, which is how a real call ends up answered by
    the mock provider.
    """
    path = Path(Settings.model_config["env_file"])
    return f"{path} ({'found' if path.is_file() else 'MISSING'})"
