"""Password hashing and JWT. None of this is hand-rolled -- bcrypt/pyjwt are
widely audited de facto standards; our job is only to use them correctly
(reasonable cost parameters, ALWAYS verify signature and expiry, explicit
algorithm instead of trusting whatever the token itself declares)."""
from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import secrets
import uuid
from dataclasses import dataclass

import bcrypt
import jwt

from .config import Settings
from .schemas import PLATFORM_ROLES, TENANT_ROLES

# HS256 (symmetric HMAC): simple, sufficient for a single backend that issues
# and validates its own tokens (no need for RS256 public/private keys until a
# third party verifies tokens independently).
JWT_ALGORITHM = "HS256"

# API keys (M2M integrations, see 0034_api_keys.sql) -- fixed prefix so
# deps.py can tell "this is an API key" from "this is a JWT" without trying to
# decode it first (avoids spending time / logging noise from jwt.decode()
# against something that was never a JWT).
API_KEY_PREFIX = "omd_"
# How many secret characters (after the prefix) are shown in admin listings to
# identify the key without exposing it in full again.
API_KEY_DISPLAY_CHARS = 8


def generate_api_key() -> tuple[str, str]:
    """Returns (full_key, display_prefix). The full key is handed to the
    caller (the creation endpoint) ONCE -- never persisted, never retrievable
    again."""
    raw_secret = secrets.token_urlsafe(32)  # 256 bits of real entropy
    full_key = f"{API_KEY_PREFIX}{raw_secret}"
    display_prefix = full_key[: len(API_KEY_PREFIX) + API_KEY_DISPLAY_CHARS]
    return full_key, display_prefix


def hash_api_key(settings: Settings, full_key: str) -> str:
    """HMAC-SHA256 with a server-side pepper (api_key_pepper, DISTINCT from
    jwt_secret) -- unlike a human password, the key already has 256 bits of
    entropy, so a slow bcrypt-style hash adds no real brute-force resistance
    and would be a real per-request cost for a high-volume integration. The
    pepper means a table dump (without access to the process/environment) is
    not enough to forge keys or run a dictionary attack against stolen
    hashes."""
    return hmac.new(settings.api_key_pepper.encode("utf-8"), full_key.encode("utf-8"), hashlib.sha256).hexdigest()


def hash_password(plain: str) -> str:
    encoded = plain.encode("utf-8")
    if len(encoded) > 72:
        # bcrypt truncates/rejects inputs over 72 bytes (not characters -- a
        # short password with accents/emoji can exceed 72 bytes in UTF-8).
        # Fail explicitly instead of letting bcrypt raise ValueError.
        raise ValueError("password cannot exceed 72 bytes")
    return bcrypt.hashpw(encoded, bcrypt.gensalt()).decode("ascii")


# Fixed hash (never matches a real password) used only so the "email does not
# exist" path in /auth/login does the same bcrypt work as the "email exists,
# wrong password" path -- keeps response latency from revealing whether an
# email is registered.
DUMMY_PASSWORD_HASH = bcrypt.hashpw(b"openmdvr-dummy-hash-for-timing", bcrypt.gensalt()).decode("ascii")


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("ascii"))
    except ValueError:
        # Malformed hash (should never happen with our own data, but a
        # corrupt hash must not crash login with a 500 -- it is "invalid
        # credentials" just like a wrong password).
        return False


@dataclass(frozen=True)
class TokenClaims:
    user_id: str
    tenant_id: str | None
    role: str
    is_platform_bypass: bool
    # Only non-None when role == "driver" -- binds the session to its row in
    # `drivers` (migration 0015). Sets the app.driver_id GUC that
    # driver_shift_events uses so a driver only sees their own events (see
    # deps.py get_db).
    driver_id: str | None = None
    # --- API keys (0034_api_keys.sql) ---
    # "password" (normal JWT, human login) or "api_key" -- deps.py uses this
    # to decide whether the EXTRA API key restrictions apply (read-only, tag
    # allowlist, device scoping). A normal JWT session never has any of those
    # restrictions, regardless of role.
    auth_method: str = "password"
    can_write: bool = True
    # None = no extra device scoping (everything the user can already see).
    # Tuple, not list, so TokenClaims stays hashable (frozen=True).
    allowed_device_ids: tuple[str, ...] | None = None
    api_key_id: str | None = None


def create_access_token(settings: Settings, claims: TokenClaims) -> str:
    now = dt.datetime.now(dt.timezone.utc)
    payload = {
        "sub": claims.user_id,
        "tenant_id": claims.tenant_id,
        "role": claims.role,
        "bypass": claims.is_platform_bypass,
        "driver_id": claims.driver_id,
        "iat": now,
        "exp": now + dt.timedelta(minutes=settings.jwt_expire_minutes),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=JWT_ALGORITHM)


class InvalidToken(Exception):
    pass


def decode_access_token(settings: Settings, token: str) -> TokenClaims:
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=[JWT_ALGORITHM])
    except jwt.PyJWTError as e:
        raise InvalidToken(str(e)) from e

    user_id = payload.get("sub")
    role = payload.get("role")
    if not user_id or not role:
        raise InvalidToken("missing required claims")
    # Validate the SHAPE of the claims before they reach set_config()/RLS: a
    # JWT signed with the real key (only possible if that key leaks, but
    # defense in depth is the same doctrine applied across the RLS layer) with
    # a non-UUID tenant_id had no rejection here -- it reached a ::uuid cast
    # in the RLS policy, which fails with a raw 500 instead of a clean 401. A
    # token should never carry a role outside the known set either.
    if role not in TENANT_ROLES and role not in PLATFORM_ROLES:
        raise InvalidToken(f"unknown role: {role!r}")
    tenant_id = payload.get("tenant_id")
    if tenant_id is not None:
        try:
            uuid.UUID(str(tenant_id))
        except ValueError:
            raise InvalidToken("malformed tenant_id")

    # user_id (claim "sub") and driver_id -- same gap as tenant_id above.
    # Since user_id feeds a GUC (app.user_id, migration 0031) read INSIDE an
    # RLS policy (app_can_view_device, 0032), a non-UUID value reaches that
    # policy's ::uuid cast and fails with a raw 500 instead of a clean 401.
    # Not exploitable without the signing secret; same defense in depth as
    # the rest of this file.
    try:
        uuid.UUID(str(user_id))
    except ValueError:
        raise InvalidToken("malformed user_id")
    driver_id = payload.get("driver_id")
    if driver_id is not None:
        try:
            uuid.UUID(str(driver_id))
        except ValueError:
            raise InvalidToken("malformed driver_id")

    return TokenClaims(
        user_id=user_id,
        tenant_id=tenant_id,
        role=role,
        is_platform_bypass=bool(payload.get("bypass", False)),
        driver_id=driver_id,
    )
