"""Platform operational settings unrelated to billing -- the "device alive /
seen now" threshold (`platform_monitoring_settings`, migration 0028), the map
provider override, and device health. Unlike platform_billing_settings
(bypass-only both ways), READING the monitoring threshold is open to any
authenticated session: a tenant_admin/operator/viewer/driver needs the same
threshold as the platform to render the status of THEIR OWN devices
consistently -- an integer number of seconds is not sensitive. Only EDITING it
is bypass-only, same operational rule as a tenant's `max_live_view_seconds`."""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from psycopg import AsyncConnection
from psycopg.errors import CheckViolation

from ..deps import get_db, require_bypass, require_super_admin
from ..schemas import (
    PlatformMapSettingsOut,
    PlatformMapSettingsUpdate,
    PlatformMonitoringSettingsOut,
    PlatformMonitoringSettingsUpdate,
)
from ..security import TokenClaims

router = APIRouter(prefix="/platform", tags=["platform"])

_SETTINGS_COLUMNS = "device_offline_threshold_seconds, updated_at"

_MAP_SETTINGS_COLUMNS = """
    m.active_provider, u.email, m.forced_at, m.updated_at
"""
_MAP_SETTINGS_FROM_JOIN = "FROM platform_map_settings m LEFT JOIN users u ON u.id = m.forced_by"


def _map_settings_out(row) -> PlatformMapSettingsOut:
    return PlatformMapSettingsOut(
        active_provider=row[0],
        forced_by_email=row[1],
        forced_at=row[2].isoformat() if row[2] else None,
        updated_at=row[3].isoformat(),
    )


def _settings_out(row) -> PlatformMonitoringSettingsOut:
    return PlatformMonitoringSettingsOut(device_offline_threshold_seconds=row[0], updated_at=row[1].isoformat())


@router.get("/monitoring-settings", response_model=PlatformMonitoringSettingsOut)
async def get_monitoring_settings(
    conn: AsyncConnection = Depends(get_db),
    # No extra role dependency on purpose -- get_db already requires a valid
    # JWT (get_current_user), and THAT is all this endpoint needs: any
    # authenticated session, including a driver, can read this value.
) -> PlatformMonitoringSettingsOut:
    row = await (await conn.execute(f"SELECT {_SETTINGS_COLUMNS} FROM platform_monitoring_settings")).fetchone()
    return _settings_out(row)


@router.patch("/monitoring-settings", response_model=PlatformMonitoringSettingsOut)
async def update_monitoring_settings(
    body: PlatformMonitoringSettingsUpdate,
    conn: AsyncConnection = Depends(get_db),
    _: TokenClaims = Depends(require_bypass),
) -> PlatformMonitoringSettingsOut:
    try:
        row = await (
            await conn.execute(
                f"""UPDATE platform_monitoring_settings
                    SET device_offline_threshold_seconds = %s
                    RETURNING {_SETTINGS_COLUMNS}""",
                (body.device_offline_threshold_seconds,),
            )
        ).fetchone()
    except CheckViolation:
        # Defense in depth -- Field(gt=0, le=...) in schemas.py is the real
        # barrier, but a bound copied without checking the actual column has
        # caused a raw 500 before (platform_billing_settings); better a clean
        # 422 if it ever drifts from the column's real CHECK.
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "threshold out of range")
    return _settings_out(row)


@router.get("/map-settings", response_model=PlatformMapSettingsOut)
async def get_map_settings(
    conn: AsyncConnection = Depends(get_db),
    # No extra role dependency, same as monitoring-settings: any session with
    # a valid JWT needs to know whether a provider is forced -- ALL tenants
    # must see the same map.
) -> PlatformMapSettingsOut:
    row = await (
        await conn.execute(f"SELECT {_MAP_SETTINGS_COLUMNS} {_MAP_SETTINGS_FROM_JOIN}")
    ).fetchone()
    return _map_settings_out(row)


@router.patch("/map-settings", response_model=PlatformMapSettingsOut)
async def update_map_settings(
    body: PlatformMapSettingsUpdate,
    conn: AsyncConnection = Depends(get_db),
    user: TokenClaims = Depends(require_super_admin),
) -> PlatformMapSettingsOut:
    # Unlike monitoring-settings (require_bypass, support operational work),
    # forcing the map provider for the WHOLE platform is an infrastructure
    # decision -- require_super_admin, same as creating/editing billing_plans
    # (RLS only requires bypass; this is the layer that actually separates
    # super_admin from support).
    #
    # 'auto' clears forced_by/forced_at, any other value stamps them --
    # preserves the invariant documented in migration 0030 ("NULL when
    # 'auto', the expected state").
    if body.active_provider == "auto":
        row = await (
            await conn.execute(
                """UPDATE platform_map_settings
                   SET active_provider = 'auto', forced_by = NULL, forced_at = NULL
                   RETURNING id"""
            )
        ).fetchone()
    else:
        row = await (
            await conn.execute(
                """UPDATE platform_map_settings
                   SET active_provider = %s, forced_by = %s, forced_at = now()
                   RETURNING id""",
                (body.active_provider, user.user_id),
            )
        ).fetchone()
    final_row = await (
        await conn.execute(f"SELECT {_MAP_SETTINGS_COLUMNS} {_MAP_SETTINGS_FROM_JOIN} WHERE m.id = %s", (row[0],))
    ).fetchone()
    return _map_settings_out(final_row)


# --- Device health (0053) ----------------------------------------------------
#
# OPERATIONAL device problems (data wasted on retries, native photo that fell
# back to the expensive video path, clips recovered late...) that the platform
# needs to see in order to act -- distinct from each tenant's alarms.
# Deduplicated in the database: the same problem on the same device is ONE row
# with a counter. RLS is bypass-only; require_bypass is the API layer. The
# ACTIONS (reboot, configure) reuse the existing configuration-command
# endpoints (super_admin), never a new path.


@router.get("/device-health")
async def list_device_health(
    include_resolved: bool = False,
    limit: int = 100,
    conn: AsyncConnection = Depends(get_db),
    _: TokenClaims = Depends(require_bypass),
):
    limit = max(1, min(limit, 300))
    rows = await (
        await conn.execute(
            f"""SELECT h.id, h.device_id, d.label, d.protocol, t.name, h.kind, h.severity, h.title,
                       h.detail, h.occurrences, h.bytes_wasted, h.first_seen, h.last_seen, h.resolved_at
                  FROM device_health_events h
                  JOIN devices d ON d.id = h.device_id
                  JOIN tenants t ON t.id = h.tenant_id
                 {"" if include_resolved else "WHERE h.resolved_at IS NULL"}
                 ORDER BY (h.resolved_at IS NULL) DESC,
                          CASE h.severity WHEN 'critical' THEN 0 WHEN 'warning' THEN 1 ELSE 2 END,
                          h.last_seen DESC
                 LIMIT %s""",
            (limit,),
        )
    ).fetchall()
    open_count = (
        await (await conn.execute("SELECT count(*) FROM device_health_events WHERE resolved_at IS NULL")).fetchone()
    )[0]
    return {
        "open_count": open_count,
        "items": [
            {
                "id": str(r[0]),
                "device_id": str(r[1]),
                "device_label": r[2],
                "protocol": r[3],
                "tenant_name": r[4],
                "kind": r[5],
                "severity": r[6],
                "title": r[7],
                "detail": r[8],
                "occurrences": r[9],
                "bytes_wasted": r[10],
                "first_seen": r[11].isoformat(),
                "last_seen": r[12].isoformat(),
                "resolved_at": r[13].isoformat() if r[13] else None,
            }
            for r in rows
        ],
    }


@router.post("/device-health/{event_id}/resolve", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
async def resolve_device_health(
    event_id: uuid.UUID,
    conn: AsyncConnection = Depends(get_db),
    user: TokenClaims = Depends(require_bypass),
) -> None:
    row = await (
        await conn.execute(
            """UPDATE device_health_events SET resolved_at = now(), resolved_by = %s
               WHERE id = %s AND resolved_at IS NULL RETURNING id""",
            (user.user_id, event_id),
        )
    ).fetchone()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "event not found or already resolved")
