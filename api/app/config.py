"""Configuration read from environment variables. Never from a versioned file
-- in production these variables are injected by the deployment platform."""
import os
from dataclasses import dataclass
from functools import lru_cache


def _require(name: str) -> str:
    v = os.environ.get(name)
    if not v:
        raise RuntimeError(f"config: environment variable {name} is required")
    return v


@dataclass(frozen=True)
class Settings:
    pg_host: str
    pg_port: str
    pg_database: str
    pg_user: str
    pg_password: str

    jwt_secret: str
    # Short-lived on purpose: a stolen JWT stops working soon on its own
    # instead of relying solely on nobody stealing it.
    jwt_expire_minutes: int

    # Pepper for API key hashing (api/app/security.py) -- DELIBERATELY its own
    # secret, separate from jwt_secret: if one ever leaks, the other auth
    # mechanism stays protected.
    api_key_pepper: str

    jt1078_bridge_base_url: str

    # --- S3-compatible storage (Cloudflare R2, see app/storage.py) -- all
    # optional: a deployment without a configured bucket simply cannot issue
    # signed URLs (storage.generate_signed_url fails with a clear error),
    # without breaking the rest of the API.
    r2_endpoint: str | None
    r2_access_key: str | None
    r2_secret_key: str | None
    r2_bucket: str | None


@lru_cache
def get_settings() -> Settings:
    """Called explicitly where needed (never at module import time) -- so
    tests can set environment variables before the first call regardless of
    module import order. lru_cache makes it a normal singleton afterwards."""
    return Settings(
        pg_host=os.environ.get("PGHOST", "127.0.0.1"),
        pg_port=os.environ.get("PGPORT", "55432"),
        pg_database=os.environ.get("PGDATABASE", "openmdvr"),
        pg_user=os.environ.get("PGUSER", "app_user"),
        pg_password=_require("APP_USER_PASSWORD"),
        jwt_secret=_require("JWT_SECRET"),
        jwt_expire_minutes=int(os.environ.get("JWT_EXPIRE_MINUTES", "480")),
        api_key_pepper=_require("API_KEY_PEPPER"),
        jt1078_bridge_base_url=os.environ.get("JT1078_BRIDGE_BASE_URL", "http://127.0.0.1:8082"),
        r2_endpoint=os.environ.get("R2_ENDPOINT") or None,
        r2_access_key=os.environ.get("R2_ACCESS_KEY") or None,
        r2_secret_key=os.environ.get("R2_SECRET_KEY") or None,
        r2_bucket=os.environ.get("R2_BUCKET") or None,
    )
