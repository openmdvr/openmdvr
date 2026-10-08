"""GET/PATCH /platform/monitoring-settings -- configurable "device alive /
seen now" threshold (platform_monitoring_settings, migration 0028). Core
concern, unlike platform_billing_settings: READING this value must work
for ANY authenticated session (including tenant_admin, without bypass)
because a tenant needs the same rule as the platform to render the status
of its own devices -- only EDITING it is bypass-only."""
import pytest

from conftest import auth_header, login

pytestmark = pytest.mark.asyncio


async def test_default_seeded_by_migration(client, platform_users):
    token = await login(client, platform_users["super_admin"]["email"])
    resp = await client.get("/platform/monitoring-settings", headers=auth_header(token))
    assert resp.status_code == 200
    assert resp.json()["device_offline_threshold_seconds"] == 300


async def test_tenant_admin_can_read_without_bypass(client, two_tenants):
    """The central point of this endpoint: unlike EVERYTHING in
    platform_billing_settings, a tenant session (without RLS bypass) can read
    this value -- it needs the same threshold as the platform."""
    token = await login(client, two_tenants["a"]["email"])
    resp = await client.get("/platform/monitoring-settings", headers=auth_header(token))
    assert resp.status_code == 200
    assert resp.json()["device_offline_threshold_seconds"] == 300


async def test_tenant_admin_cannot_update(client, two_tenants):
    token = await login(client, two_tenants["a"]["email"])
    resp = await client.patch(
        "/platform/monitoring-settings", json={"device_offline_threshold_seconds": 600}, headers=auth_header(token)
    )
    assert resp.status_code == 403


async def test_support_can_update(client, platform_users):
    """Unlike platform_billing_settings (super_admin-only to edit), this is
    operational support work -- same rule as a tenant's max_live_view_seconds."""
    token = await login(client, platform_users["support"]["email"])
    resp = await client.patch(
        "/platform/monitoring-settings", json={"device_offline_threshold_seconds": 600}, headers=auth_header(token)
    )
    assert resp.status_code == 200
    assert resp.json()["device_offline_threshold_seconds"] == 600
    # Restore -- a global row shared between tests.
    await client.patch(
        "/platform/monitoring-settings", json={"device_offline_threshold_seconds": 300}, headers=auth_header(token)
    )


async def test_update_zero_rejected_with_422(client, platform_users):
    token = await login(client, platform_users["super_admin"]["email"])
    resp = await client.patch(
        "/platform/monitoring-settings", json={"device_offline_threshold_seconds": 0}, headers=auth_header(token)
    )
    assert resp.status_code == 422


async def test_update_above_max_rejected_with_422(client, platform_users):
    token = await login(client, platform_users["super_admin"]["email"])
    resp = await client.patch(
        "/platform/monitoring-settings",
        json={"device_offline_threshold_seconds": 999_999},
        headers=auth_header(token),
    )
    assert resp.status_code == 422
