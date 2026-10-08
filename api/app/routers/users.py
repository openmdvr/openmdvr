from __future__ import annotations

import datetime as dt
import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from psycopg import AsyncConnection
from psycopg.errors import CheckViolation, RaiseException, UniqueViolation

from ..deps import get_db, require_non_driver, require_tenant_admin, require_tenant_admin_or_super_admin
from ..schemas import (
    PLATFORM_ROLES,
    TENANT_ROLES,
    ApiKeyCreate,
    ApiKeyCreatedOut,
    ApiKeyOut,
    ApiKeyUsageOut,
    Page,
    UserCreate,
    UserDeviceAssignmentsOut,
    UserDeviceAssignmentsUpdate,
    UserNotificationSettingsOut,
    UserNotificationSettingsUpdate,
    UserOut,
    UserPasswordReset,
    UserStatusUpdate,
)
from ..config import get_settings
from ..security import TokenClaims, generate_api_key, hash_api_key, hash_password

router = APIRouter(prefix="/users", tags=["users"])
logger = logging.getLogger(__name__)


@router.post("", response_model=UserOut, status_code=201)
async def create_user(
    body: UserCreate,
    conn: AsyncConnection = Depends(get_db),
    # require_non_driver, not get_current_user: a driver would already get a
    # 403 from the "only tenant_admin can create users" branch below, but
    # rejecting it explicitly here is clearer and consistent with the other
    # fleet endpoints (see require_non_driver in deps.py).
    user: TokenClaims = Depends(require_non_driver),
) -> UserOut:
    # Role-based authorization (on top of RLS, which only guarantees the
    # tenant boundary): who may create which kind of user.
    if body.role in PLATFORM_ROLES:
        # Creating another platform account (super_admin or support) is
        # super_admin-only, bypass is not enough: `support` has RLS bypass for
        # cross-tenant read support, but letting it create another `support`
        # (or worse, a `super_admin`) would be a horizontal/vertical privilege
        # escalation that RLS alone cannot distinguish (its policy only
        # requires bypass, not a specific role).
        if user.role != "super_admin":
            raise HTTPException(status.HTTP_403_FORBIDDEN, "only super_admin can create another platform user")
        if body.tenant_id is not None:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "a platform user must not have a tenant_id")
    else:  # TENANT_ROLES (includes "driver")
        if body.tenant_id is None:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "tenant_id is required for a tenant user")
        if not user.is_platform_bypass:
            if user.role != "tenant_admin":
                raise HTTPException(status.HTTP_403_FORBIDDEN, "only tenant_admin can create users in their tenant")
            if str(body.tenant_id) != user.tenant_id:
                # Not only an app check: RLS would block it too (users_insert
                # requires tenant_id = the session's tenant), but an explicit
                # 403 is a better experience than a generic database error.
                raise HTTPException(status.HTTP_403_FORBIDDEN, "you cannot create users for another tenant")
        # driver_id required ONLY for role="driver" -- same rule as tenant_id
        # above, it depends on the VALUE of role. That the driver_id belongs
        # to the SAME tenant is guaranteed by the users_tenant_role_consistency
        # CHECK + the users_enforce_driver_tenant trigger (migration 0015), not
        # only by this validation.
        if body.role == "driver" and body.driver_id is None:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "driver_id is required for role=driver")
        if body.role != "driver" and body.driver_id is not None:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "driver_id only applies to role=driver")

    try:
        password_hash = hash_password(body.password)
    except ValueError:
        # bcrypt rejects inputs over 72 UTF-8 bytes -- a password short in
        # characters can exceed that limit with accents/emoji.
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "the password is too long")

    try:
        row = await (
            await conn.execute(
                """INSERT INTO users (tenant_id, email, password_hash, role, is_platform_bypass, driver_id)
                   VALUES (%s, %s, %s, %s, %s, %s)
                   RETURNING id, email, role, tenant_id, driver_id, status, status_changed_by, status_changed_at""",
                (
                    body.tenant_id,
                    body.email,
                    password_hash,
                    body.role,
                    body.role in PLATFORM_ROLES,
                    body.driver_id,
                ),
            )
        ).fetchone()
    except UniqueViolation:
        raise HTTPException(status.HTTP_409_CONFLICT, "a user with that email already exists, or that driver already has an account")
    except CheckViolation:
        # Defense in depth: the database CHECK (users_tenant_role_consistency)
        # is the real guarantee; this should only fire on a bug in the
        # validation above, never in normal use.
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid tenant_id/role/driver_id combination")
    except RaiseException:
        # enforce_driver_tenant_match() (migration 0014/0015) rejects a
        # driver_id that does not belong to this user's tenant_id.
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid driver_id for this tenant")

    return UserOut(
        id=row[0], email=row[1], role=row[2], tenant_id=row[3], driver_id=row[4], status=row[5],
        status_changed_by=row[6], status_changed_at=row[7].isoformat() if row[7] else None,
    )


@router.get("", response_model=Page[UserOut])
async def list_users(
    conn: AsyncConnection = Depends(get_db),
    _: TokenClaims = Depends(require_non_driver),
    search: str | None = Query(None, max_length=200),
    # Optional filter -- RLS is the real isolation (users_select), this only
    # narrows FURTHER within what RLS allows (a tenant session can never widen
    # its scope by passing another tenant_id). Lets a platform session ask for
    # ONLY one tenant's users instead of the global list.
    tenant_id: uuid.UUID | None = Query(None),
    limit: int = Query(50, ge=1, le=1000),
    offset: int = Query(0, ge=0),
) -> Page[UserOut]:
    where_parts: list[str] = []
    params: list[object] = []
    if search:
        where_parts.append("email ILIKE %s")
        params.append(f"%{search}%")
    if tenant_id is not None:
        where_parts.append("tenant_id = %s")
        params.append(tenant_id)
    where = f"WHERE {' AND '.join(where_parts)}" if where_parts else ""

    total_row = await (await conn.execute(f"SELECT count(*) FROM users {where}", params)).fetchone()
    total = total_row[0] if total_row else 0

    rows = await (
        await conn.execute(
            f"""SELECT id, email, role, tenant_id, driver_id, status, status_changed_by, status_changed_at
                FROM users {where} ORDER BY email LIMIT %s OFFSET %s""",
            [*params, limit, offset],
        )
    ).fetchall()
    items = [
        UserOut(
            id=r[0], email=r[1], role=r[2], tenant_id=r[3], driver_id=r[4], status=r[5],
            status_changed_by=r[6], status_changed_at=r[7].isoformat() if r[7] else None,
        )
        for r in rows
    ]
    return Page(items=items, total=total, limit=limit, offset=offset)


@router.patch("/{user_id}/status", response_model=UserOut)
async def update_user_status(
    user_id: uuid.UUID,
    body: UserStatusUpdate,
    conn: AsyncConnection = Depends(get_db),
    # Same require_non_driver + explicit role check as create_user() above --
    # deactivating/reactivating an account deserves exactly the same
    # permission boundary as creating it. This endpoint NEVER deletes the row
    # (soft delete via status), but it DOES take effect immediately:
    # deps.py::assert_session_active and api_key_auth.py already check
    # users.status on EVERY request, so deactivating cuts that account's
    # access (JWT and API keys) on the NEXT request, without waiting for the
    # token to expire.
    admin: TokenClaims = Depends(require_non_driver),
) -> UserOut:
    if str(user_id) == admin.user_id:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "you cannot change the status of your own account")

    # `users` RLS (users_select) already makes this a 404 if the user belongs
    # to another tenant -- but we need their ROLE to pick the right permission
    # (same rule as create_user: platform vs. tenant accounts are not the
    # same boundary).
    target_row = await (await conn.execute("SELECT role, tenant_id FROM users WHERE id = %s", (user_id,))).fetchone()
    if target_row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "user not found")
    target_role, target_tenant_id = target_row

    if target_role in PLATFORM_ROLES:
        # Changing the status of ANOTHER platform account (super_admin/
        # support) is super_admin-only -- same rule as creating it: `support`
        # must not be able to disable another `support` (or a `super_admin`),
        # a horizontal escalation/DoS between platform accounts that RLS alone
        # cannot distinguish (its policy only requires bypass).
        if admin.role != "super_admin":
            raise HTTPException(
                status.HTTP_403_FORBIDDEN, "only super_admin can change the status of a platform account"
            )
    else:
        if not admin.is_platform_bypass:
            if admin.role != "tenant_admin":
                raise HTTPException(
                    status.HTTP_403_FORBIDDEN, "only tenant_admin can change the status of users in their tenant"
                )
            if str(target_tenant_id) != admin.tenant_id:
                raise HTTPException(status.HTTP_403_FORBIDDEN, "you cannot change the status of users in another tenant")

    row = await (
        await conn.execute(
            """UPDATE users SET status = %s, status_changed_by = %s, status_changed_at = now()
               WHERE id = %s
               RETURNING id, email, role, tenant_id, driver_id, status, status_changed_by, status_changed_at""",
            (body.status, admin.user_id, user_id),
        )
    ).fetchone()
    return UserOut(
        id=row[0], email=row[1], role=row[2], tenant_id=row[3], driver_id=row[4], status=row[5],
        status_changed_by=row[6], status_changed_at=row[7].isoformat() if row[7] else None,
    )


@router.post("/{user_id}/reset-password", response_model=UserOut)
async def reset_user_password(
    user_id: uuid.UUID,
    body: UserPasswordReset,
    conn: AsyncConnection = Depends(get_db),
    # Same permission rule as update_user_status above (activating/
    # deactivating and resetting a password are the same class of
    # administrative action on ANOTHER account). The admin CHOOSES the new
    # password (no email sending, this project has no such service) and
    # communicates it to the affected user outside the platform.
    admin: TokenClaims = Depends(require_non_driver),
) -> UserOut:
    target_row = await (await conn.execute("SELECT role, tenant_id FROM users WHERE id = %s", (user_id,))).fetchone()
    if target_row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "user not found")
    target_role, target_tenant_id = target_row

    if target_role in PLATFORM_ROLES:
        # Resetting the password of ANOTHER platform account is
        # super_admin-only -- same rule as creating it / changing its status:
        # `support` must not be able to take over another support or
        # super_admin account.
        if admin.role != "super_admin":
            raise HTTPException(
                status.HTTP_403_FORBIDDEN, "only super_admin can reset the password of a platform account"
            )
    else:
        if not admin.is_platform_bypass:
            if admin.role != "tenant_admin":
                raise HTTPException(
                    status.HTTP_403_FORBIDDEN, "only tenant_admin can reset passwords in their tenant"
                )
            if str(target_tenant_id) != admin.tenant_id:
                raise HTTPException(status.HTTP_403_FORBIDDEN, "you cannot reset passwords in another tenant")
            if target_role == "tenant_admin":
                # A tenant_admin can reset the password of their own team
                # (operators/viewers/drivers) but NEVER another tenant_admin's
                # -- that is reserved to the platform.
                raise HTTPException(
                    status.HTTP_403_FORBIDDEN, "a tenant_admin cannot reset another tenant_admin's password"
                )
        # Accepted design trade-off: `support` (RLS bypass, not super_admin)
        # CAN reset a tenant_admin's password under this rule -- the same
        # pattern flagged as a risk for ISSUING a new API key (a support-issued
        # key could outlive the support account being disabled). Here the
        # affected user can detect the change (their password stops working)
        # and a super_admin can revoke it by disabling the `support` account,
        # unlike a silent API key -- hence the trade-off was accepted instead
        # of excluding `support` as with API keys.

    try:
        password_hash = hash_password(body.new_password)
    except ValueError:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "the password is too long")

    row = await (
        await conn.execute(
            """UPDATE users SET password_hash = %s WHERE id = %s
               RETURNING id, email, role, tenant_id, driver_id, status, status_changed_by, status_changed_at""",
            (password_hash, user_id),
        )
    ).fetchone()
    logger.warning("password reset by user_id=%s (role=%s) for user_id=%s", admin.user_id, admin.role, user_id)
    return UserOut(
        id=row[0], email=row[1], role=row[2], tenant_id=row[3], driver_id=row[4], status=row[5],
        status_changed_by=row[6], status_changed_at=row[7].isoformat() if row[7] else None,
    )


# --- Device/group assignment + notification preference ---
# See infra/postgres/migrations/0031_device_groups_and_assignments.sql -- only
# applies to tenant_operator/tenant_viewer (tenant_admin always sees the whole
# tenant, no assignment needed).
_ASSIGNABLE_ROLES = {"tenant_operator", "tenant_viewer"}


@router.get("/{user_id}/device-assignments", response_model=UserDeviceAssignmentsOut)
async def get_user_device_assignments(
    user_id: uuid.UUID,
    conn: AsyncConnection = Depends(get_db),
    # require_tenant_admin, not require_non_driver (same rule as
    # device_groups.py::list_device_group_members): this exposes raw device_ids
    # without going through app_can_view_device -- a tenant_operator/
    # tenant_viewer could otherwise enumerate real UUIDs of devices they
    # cannot see through any other endpoint.
    _: TokenClaims = Depends(require_tenant_admin),
) -> UserDeviceAssignmentsOut:
    # `users` RLS (users_select) already makes this a 404 if user_id belongs to
    # another tenant -- no separate tenant check needed here.
    user_row = await (await conn.execute("SELECT id FROM users WHERE id = %s", (user_id,))).fetchone()
    if user_row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "user not found")
    device_rows = await (
        await conn.execute("SELECT device_id FROM user_device_assignments WHERE user_id = %s", (user_id,))
    ).fetchall()
    group_rows = await (
        await conn.execute("SELECT device_group_id FROM user_device_group_assignments WHERE user_id = %s", (user_id,))
    ).fetchall()
    return UserDeviceAssignmentsOut(
        device_ids=[r[0] for r in device_rows], device_group_ids=[r[0] for r in group_rows]
    )


@router.put("/{user_id}/device-assignments", response_model=UserDeviceAssignmentsOut)
async def replace_user_device_assignments(
    user_id: uuid.UUID,
    body: UserDeviceAssignmentsUpdate,
    conn: AsyncConnection = Depends(get_db),
    _: TokenClaims = Depends(require_tenant_admin),
) -> UserDeviceAssignmentsOut:
    async with conn.transaction():
        user_row = await (await conn.execute("SELECT tenant_id, role FROM users WHERE id = %s", (user_id,))).fetchone()
        if user_row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "user not found")
        tenant_id, role = user_row
        if role not in _ASSIGNABLE_ROLES:
            # tenant_admin already sees everything (an assignment would have no
            # effect); driver/platform do not take part in this model at all
            # (see require_non_driver and migration 0031's RLS policies, none
            # of which reach a driver).
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "device assignment only applies to tenant_operator/tenant_viewer",
            )

        await conn.execute("DELETE FROM user_device_assignments WHERE user_id = %s", (user_id,))
        await conn.execute("DELETE FROM user_device_group_assignments WHERE user_id = %s", (user_id,))
        try:
            for device_id in dict.fromkeys(body.device_ids):
                await conn.execute(
                    "INSERT INTO user_device_assignments (user_id, device_id, tenant_id) VALUES (%s, %s, %s)",
                    (user_id, device_id, tenant_id),
                )
            for group_id in dict.fromkeys(body.device_group_ids):
                await conn.execute(
                    "INSERT INTO user_device_group_assignments (user_id, device_group_id, tenant_id) VALUES (%s, %s, %s)",
                    (user_id, group_id, tenant_id),
                )
        except RaiseException:
            # enforce_user_device_assignment_tenant_match()/
            # enforce_user_device_group_assignment_tenant_match() (migration
            # 0031) reject a device_id/device_group_id from another tenant.
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "one of the devices or groups does not belong to this tenant",
            )

    return UserDeviceAssignmentsOut(device_ids=body.device_ids, device_group_ids=body.device_group_ids)


@router.get("/{user_id}/notification-settings", response_model=UserNotificationSettingsOut)
async def get_user_notification_settings(
    user_id: uuid.UUID,
    conn: AsyncConnection = Depends(get_db),
    _: TokenClaims = Depends(require_non_driver),
) -> UserNotificationSettingsOut:
    # If the user never configured anything, user_notification_settings has
    # no row (see the table comment, migration 0031) -- the
    # UserNotificationSettingsOut defaults already match the column DEFAULTs,
    # so a missing row and an "all defaults" row are indistinguishable to the
    # client, as they should be.
    user_row = await (await conn.execute("SELECT id FROM users WHERE id = %s", (user_id,))).fetchone()
    if user_row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "user not found")
    row = await (
        await conn.execute(
            "SELECT in_app_enabled, email_enabled FROM user_notification_settings WHERE user_id = %s",
            (user_id,),
        )
    ).fetchone()
    if row is None:
        return UserNotificationSettingsOut()
    return UserNotificationSettingsOut(in_app_enabled=row[0], email_enabled=row[1])


@router.patch("/{user_id}/notification-settings", response_model=UserNotificationSettingsOut)
async def update_user_notification_settings(
    user_id: uuid.UUID,
    body: UserNotificationSettingsUpdate,
    conn: AsyncConnection = Depends(get_db),
    _: TokenClaims = Depends(require_tenant_admin),
) -> UserNotificationSettingsOut:
    user_row = await (await conn.execute("SELECT tenant_id FROM users WHERE id = %s", (user_id,))).fetchone()
    if user_row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "user not found")
    tenant_id = user_row[0]
    # NOTE: EXCLUDED.in_app_enabled cannot express "leave this field alone" --
    # it already came out of the VALUES clause with its own
    # COALESCE(..., true/false) applied, so it is never NULL and would always
    # overwrite the existing row. The raw body parameters (possibly None) are
    # repeated in the DO UPDATE branch to distinguish "omitted" (keep what was
    # there) from an explicit "set to false".
    row = await (
        await conn.execute(
            """INSERT INTO user_notification_settings (user_id, tenant_id, in_app_enabled, email_enabled)
               VALUES (%s, %s, COALESCE(%s, true), COALESCE(%s, false))
               ON CONFLICT (user_id) DO UPDATE
                   SET in_app_enabled = COALESCE(%s, user_notification_settings.in_app_enabled),
                       email_enabled = COALESCE(%s, user_notification_settings.email_enabled)
               RETURNING in_app_enabled, email_enabled""",
            (user_id, tenant_id, body.in_app_enabled, body.email_enabled, body.in_app_enabled, body.email_enabled),
        )
    ).fetchone()
    return UserNotificationSettingsOut(in_app_enabled=row[0], email_enabled=row[1])


# --- API keys (0034_api_keys.sql) -- M2M integrations that authenticate AS
# user_id (same role/tenant/real device assignment), narrowed by can_write +
# allowed_device_ids. See deps.py::get_current_user and api_key_auth.py for
# the per-request enforcement; only management lives here
# (create/list/revoke/audit), always tenant_admin or platform -- not even the
# key's owner can self-manage it (same rule as device-assignments/
# notification-settings). ---
_API_KEY_SELECT_COLUMNS = (
    "id, name, key_prefix, can_write, allowed_device_ids, created_at, expires_at, revoked_at, last_used_at, "
    "created_by, revoked_by"
)


def _api_key_out(row) -> ApiKeyOut:
    return ApiKeyOut(
        id=row[0], name=row[1], key_prefix=row[2], can_write=row[3],
        # row[4] as-is (never `or None`): NULL and [] are DISTINCT states --
        # NULL is "unscoped" (sees everything the user already sees), [] is a
        # key deliberately scoped to no device. Conflating them would show a
        # "no devices" key as "unscoped" in the admin panel.
        allowed_device_ids=row[4],
        created_at=row[5].isoformat(), expires_at=row[6].isoformat(),
        revoked_at=row[7].isoformat() if row[7] else None,
        last_used_at=row[8].isoformat() if row[8] else None,
        # created_by/revoked_by: always exposed so a tenant_admin can tell a
        # key in their panel was issued by a platform session, not by them
        # (ids, not emails, to avoid a new JOIN in an already narrow endpoint).
        created_by=row[9] if row[9] else None, revoked_by=row[10] if row[10] else None,
    )


@router.post("/{user_id}/api-keys", response_model=ApiKeyCreatedOut, status_code=201)
async def create_api_key(
    user_id: uuid.UUID,
    body: ApiKeyCreate,
    conn: AsyncConnection = Depends(get_db),
    # require_tenant_admin_or_super_admin, NOT require_tenant_admin: issuing an
    # API key is issuing a new credential -- `support` (RLS bypass, not
    # super_admin) must not be able to create credentials for OTHER users,
    # same rule as "support cannot create users" in create_user() above.
    admin: TokenClaims = Depends(require_tenant_admin_or_super_admin),
) -> ApiKeyCreatedOut:
    # `users` RLS (users_select) already makes this a 404 if user_id belongs to
    # another tenant -- no separate tenant check needed.
    user_row = await (await conn.execute("SELECT tenant_id FROM users WHERE id = %s", (user_id,))).fetchone()
    if user_row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "user not found")
    tenant_id = user_row[0]
    if tenant_id is None:
        # The enforce_api_key_tenant_match trigger would already reject it
        # (500 via RaiseException), but an explicit 422 from this endpoint is a
        # better error experience.
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "API keys are a tenant feature -- they cannot be issued for a platform account",
        )

    full_key, display_prefix = generate_api_key()
    key_hash = hash_api_key(get_settings(), full_key)
    expires_at = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=body.expires_in_days)

    try:
        row = await (
            await conn.execute(
                f"""INSERT INTO api_keys (tenant_id, user_id, name, key_prefix, key_hash, can_write,
                                           allowed_device_ids, created_by, expires_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING {_API_KEY_SELECT_COLUMNS}""",
                (
                    tenant_id, user_id, body.name, display_prefix, key_hash, body.can_write,
                    body.allowed_device_ids, admin.user_id, expires_at,
                ),
            )
        ).fetchone()
    except RaiseException:
        # enforce_api_key_tenant_match() (migration 0034) rejects a device in
        # allowed_device_ids that does not belong to the user's tenant.
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "one of the devices does not belong to this tenant")

    out = _api_key_out(row)
    return ApiKeyCreatedOut(**out.model_dump(), raw_key=full_key)


@router.get("/{user_id}/api-keys", response_model=Page[ApiKeyOut])
async def list_api_keys(
    user_id: uuid.UUID,
    conn: AsyncConnection = Depends(get_db),
    _: TokenClaims = Depends(require_tenant_admin),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> Page[ApiKeyOut]:
    user_row = await (await conn.execute("SELECT id FROM users WHERE id = %s", (user_id,))).fetchone()
    if user_row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "user not found")
    total_row = await (
        await conn.execute("SELECT count(*) FROM api_keys WHERE user_id = %s", (user_id,))
    ).fetchone()
    total = total_row[0] if total_row else 0
    rows = await (
        await conn.execute(
            f"""SELECT {_API_KEY_SELECT_COLUMNS} FROM api_keys WHERE user_id = %s
                ORDER BY created_at DESC LIMIT %s OFFSET %s""",
            (user_id, limit, offset),
        )
    ).fetchall()
    return Page(items=[_api_key_out(r) for r in rows], total=total, limit=limit, offset=offset)


@router.post("/{user_id}/api-keys/{key_id}/revoke", response_model=ApiKeyOut)
async def revoke_api_key(
    user_id: uuid.UUID,
    key_id: uuid.UUID,
    conn: AsyncConnection = Depends(get_db),
    admin: TokenClaims = Depends(require_tenant_admin),
) -> ApiKeyOut:
    # Explicit WHERE on user_id in addition to key_id -- prevents revoking (or
    # confirming the existence of) ANOTHER user's key in the same tenant via
    # this URL if someone passes a foreign key_id.
    row = await (
        await conn.execute(
            f"""UPDATE api_keys SET revoked_at = COALESCE(revoked_at, now()), revoked_by = %s
                WHERE id = %s AND user_id = %s
                RETURNING {_API_KEY_SELECT_COLUMNS}""",
            (admin.user_id, key_id, user_id),
        )
    ).fetchone()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "API key not found")
    return _api_key_out(row)


@router.get("/{user_id}/api-keys/{key_id}/usage", response_model=Page[ApiKeyUsageOut])
async def list_api_key_usage(
    user_id: uuid.UUID,
    key_id: uuid.UUID,
    conn: AsyncConnection = Depends(get_db),
    _: TokenClaims = Depends(require_tenant_admin),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> Page[ApiKeyUsageOut]:
    key_row = await (
        await conn.execute("SELECT id FROM api_keys WHERE id = %s AND user_id = %s", (key_id, user_id))
    ).fetchone()
    if key_row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "API key not found")
    total_row = await (
        await conn.execute("SELECT count(*) FROM api_key_usage_log WHERE api_key_id = %s", (key_id,))
    ).fetchone()
    total = total_row[0] if total_row else 0
    rows = await (
        await conn.execute(
            """SELECT occurred_at, method, path, status_code, ip_address FROM api_key_usage_log
               WHERE api_key_id = %s ORDER BY occurred_at DESC LIMIT %s OFFSET %s""",
            (key_id, limit, offset),
        )
    ).fetchall()
    items = [
        ApiKeyUsageOut(
            occurred_at=r[0].isoformat(), method=r[1], path=r[2], status_code=r[3],
            ip_address=str(r[4]) if r[4] is not None else None,
        )
        for r in rows
    ]
    return Page(items=items, total=total, limit=limit, offset=offset)
