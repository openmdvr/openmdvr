"""Per-tenant gps_positions retention (0019_gps_retention.sql):
tenants.gps_retention_days (a plan attribute, bypass-only) +
enforce_gps_position_retention() (a PROCEDURE registered via add_job, but
callable directly with CALL to test it without waiting for the
scheduler). The core concern, as always in this project: ONE tenant's
window must never delete or over-retain ANOTHER tenant's data."""
from datetime import datetime, timedelta, timezone

import pytest

from conftest import auth_header, login

pytestmark = pytest.mark.asyncio


async def _insert_position_at(pool, tenant_id, device_id, when):
    from app import db as db_module

    async with db_module.tenant_scoped_connection(pool, tenant_id=None, bypass=True) as conn:
        await conn.execute(
            "SELECT insert_gps_position(%s, %s, %s, 19.0, -99.0, NULL, NULL, NULL, NULL)",
            (tenant_id, device_id, when),
        )


async def _call_retention_job(pool):
    from app import db as db_module

    async with db_module.tenant_scoped_connection(pool, tenant_id=None, bypass=True) as conn:
        await conn.execute("CALL enforce_gps_position_retention(0, '{}'::jsonb)")


async def _count_positions(pool, tenant_id, device_id):
    from app import db as db_module

    async with db_module.tenant_scoped_connection(pool, tenant_id=None, bypass=True) as conn:
        row = await (
            await conn.execute(
                "SELECT count(*) FROM gps_positions_v WHERE tenant_id = %s AND device_id = %s",
                (tenant_id, device_id),
            )
        ).fetchone()
        return row[0]


async def test_tenant_defaults_to_90_days(client, two_tenants):
    token = await login(client, two_tenants["a"]["email"])
    resp = await client.get("/tenants", params={"search": ""}, headers=auth_header(token))
    row = next(t for t in resp.json()["items"] if t["id"] == str(two_tenants["a"]["tenant_id"]))
    assert row["gps_retention_days"] == 90


async def test_bypass_can_update_gps_retention_days(client, two_tenants, platform_users):
    token = await login(client, platform_users["super_admin"]["email"])
    tenant_id = two_tenants["a"]["tenant_id"]
    resp = await client.patch(f"/tenants/{tenant_id}", json={"gps_retention_days": 30}, headers=auth_header(token))
    assert resp.status_code == 200, resp.text
    assert resp.json()["gps_retention_days"] == 30


async def test_tenant_admin_cannot_update_gps_retention_days(client, two_tenants):
    """gps_retention_days lives in TenantUpdate (bypass-only), not in
    TenantSettingsUpdate -- a regular tenant_admin still cannot change their
    own retention window (it is a plan attribute)."""
    token = await login(client, two_tenants["a"]["email"])
    tenant_id = two_tenants["a"]["tenant_id"]
    resp = await client.patch(f"/tenants/{tenant_id}", json={"gps_retention_days": 7}, headers=auth_header(token))
    assert resp.status_code == 403


async def test_retention_job_respects_each_tenants_own_window(client, two_tenants, platform_users, pool):
    """The central test: tenant A with a short window, tenant B with a long
    window, each with an old and a recent position. The job must delete ONLY
    what applies to EACH tenant according to its own configuration -- never
    apply one tenant's window to the other."""
    platform_token = await login(client, platform_users["super_admin"]["email"])
    tenant_a, tenant_b = two_tenants["a"]["tenant_id"], two_tenants["b"]["tenant_id"]
    device_a, device_b = two_tenants["a"]["device_id"], two_tenants["b"]["device_id"]

    # A: short retention (7 days). B: stays on the default (90 days).
    resp = await client.patch(
        f"/tenants/{tenant_a}", json={"gps_retention_days": 7}, headers=auth_header(platform_token)
    )
    assert resp.status_code == 200

    now = datetime.now(timezone.utc)
    old = now - timedelta(days=30)  # older than A's window, newer than B's
    recent = now - timedelta(hours=1)

    await _insert_position_at(pool, tenant_a, device_a, old)
    await _insert_position_at(pool, tenant_a, device_a, recent)
    await _insert_position_at(pool, tenant_b, device_b, old)
    await _insert_position_at(pool, tenant_b, device_b, recent)

    await _call_retention_job(pool)

    # A (7-day window): the 30-day-old position must be gone, the recent one not.
    assert await _count_positions(pool, tenant_a, device_a) == 1
    # B (90-day window, default): BOTH must remain -- 30 days is within its window.
    assert await _count_positions(pool, tenant_b, device_b) == 2


async def test_retention_job_is_idempotent(client, two_tenants, pool):
    """Running it twice in a row must not fail or over-delete -- there is
    nothing older to delete the second time."""
    tenant_id, device_id = two_tenants["a"]["tenant_id"], two_tenants["a"]["device_id"]
    recent = datetime.now(timezone.utc) - timedelta(hours=1)
    await _insert_position_at(pool, tenant_id, device_id, recent)

    await _call_retention_job(pool)
    await _call_retention_job(pool)

    assert await _count_positions(pool, tenant_id, device_id) == 1
