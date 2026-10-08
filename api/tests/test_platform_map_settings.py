"""GET/PATCH /platform/map-settings -- manual override of the map tile
provider (platform_map_settings, migration 0030). Core concern, DIFFERENT
from platform_monitoring_settings: READING is just as open (any
authenticated session), but EDITING is super_admin-only (not even
support), because forcing the map provider for the WHOLE platform is an
infrastructure decision, not routine operational work -- same rule as
creating/editing billing_plans."""
import pytest

from conftest import auth_header, login

pytestmark = pytest.mark.asyncio


async def test_default_seeded_by_migration_is_auto(client, platform_users):
    token = await login(client, platform_users["super_admin"]["email"])
    resp = await client.get("/platform/map-settings", headers=auth_header(token))
    assert resp.status_code == 200
    body = resp.json()
    assert body["active_provider"] == "auto"
    assert body["forced_by_email"] is None
    assert body["forced_at"] is None


async def test_tenant_admin_can_read_without_bypass(client, two_tenants):
    """Same as monitoring-settings: any tenant session needs to know whether a
    provider is forced, to render the SAME map as the rest of the platform."""
    token = await login(client, two_tenants["a"]["email"])
    resp = await client.get("/platform/map-settings", headers=auth_header(token))
    assert resp.status_code == 200
    assert resp.json()["active_provider"] == "auto"


async def test_tenant_admin_cannot_update(client, two_tenants):
    token = await login(client, two_tenants["a"]["email"])
    resp = await client.patch("/platform/map-settings", json={"active_provider": "esri"}, headers=auth_header(token))
    assert resp.status_code == 403


async def test_support_cannot_update(client, platform_users):
    """The central point of this endpoint, unlike monitoring-settings: support
    DOES bypass RLS but is NOT super_admin -- it must be rejected just like a
    tenant_admin."""
    token = await login(client, platform_users["support"]["email"])
    resp = await client.patch("/platform/map-settings", json={"active_provider": "esri"}, headers=auth_header(token))
    assert resp.status_code == 403


async def test_super_admin_can_force_provider_and_records_who(client, platform_users):
    token = await login(client, platform_users["super_admin"]["email"])
    try:
        resp = await client.patch(
            "/platform/map-settings", json={"active_provider": "esri"}, headers=auth_header(token)
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["active_provider"] == "esri"
        assert body["forced_by_email"] == platform_users["super_admin"]["email"]
        assert body["forced_at"] is not None

        # Another (tenant) session must see the SAME forced provider.
        get_resp = await client.get("/platform/map-settings", headers=auth_header(token))
        assert get_resp.json()["active_provider"] == "esri"
    finally:
        # Restore -- a global row shared between tests.
        await client.patch("/platform/map-settings", json={"active_provider": "auto"}, headers=auth_header(token))


async def test_reverting_to_auto_clears_forced_by_and_forced_at(client, platform_users):
    """Bug found by test_default_seeded_by_migration_is_auto: a first attempt
    ALWAYS stamped forced_by/forced_at, even when going back to 'auto' --
    breaking the invariant documented by migration 0030 itself ("NULL on
    'auto', the expected state"). Without this fix, once ANY session forced a
    provider even once, "back to automatic" would never look like the
    pristine state again."""
    token = await login(client, platform_users["super_admin"]["email"])
    await client.patch("/platform/map-settings", json={"active_provider": "esri"}, headers=auth_header(token))

    resp = await client.patch("/platform/map-settings", json={"active_provider": "auto"}, headers=auth_header(token))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["active_provider"] == "auto"
    assert body["forced_by_email"] is None
    assert body["forced_at"] is None


async def test_invalid_provider_rejected_with_422(client, platform_users):
    token = await login(client, platform_users["super_admin"]["email"])
    resp = await client.patch(
        "/platform/map-settings", json={"active_provider": "google_maps"}, headers=auth_header(token)
    )
    assert resp.status_code == 422
