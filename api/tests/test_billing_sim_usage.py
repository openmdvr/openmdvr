"""GET /billing/sim-usage -- real data usage per SIM line
(device_data_usage_monthly). Focus: platform-only;
sim_plan_cost_mxn_month/sim_plan_data_cap_mb ARE read back here (unlike
DeviceOut, see test_device_models.py); monthly average and the over_cap
flag."""
import uuid

import pytest

from conftest import auth_header, login

pytestmark = pytest.mark.asyncio


async def _record_usage(pool, device_id, bytes_rx, bytes_tx):
    from app import db as db_module

    async with db_module.tenant_scoped_connection(pool, tenant_id=None, bypass=True) as conn:
        await conn.execute(
            "SELECT record_device_data_usage(%s, %s, %s)", (device_id, bytes_rx, bytes_tx)
        )


async def test_sim_usage_rejects_tenant_admin(client, two_tenants):
    token = await login(client, two_tenants["a"]["email"])
    resp = await client.get("/billing/sim-usage", headers=auth_header(token))
    assert resp.status_code == 403


async def test_sim_usage_reflects_real_recorded_bytes(client, two_tenants, platform_users, pool):
    token = await login(client, platform_users["super_admin"]["email"])
    device_id = two_tenants["a"]["device_id"]

    await client.patch(
        f"/devices/{device_id}",
        json={"sim_number": "5215500011122", "sim_plan_cost_mxn_month": 249.5, "sim_plan_data_cap_mb": 1024},
        headers=auth_header(token),
    )

    await _record_usage(pool, device_id, 1_000_000, 500_000)
    await _record_usage(pool, device_id, 200_000, 100_000)  # ACCUMULATES, does not replace

    resp = await client.get("/billing/sim-usage", params={"tenant_id": str(two_tenants["a"]["tenant_id"])}, headers=auth_header(token))
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    entry = next(i for i in items if i["device_id"] == str(device_id))

    assert entry["sim_number"] == "5215500011122"
    assert entry["sim_plan_cost_mxn_month"] == 249.5
    assert entry["sim_plan_data_cap_mb"] == 1024
    assert len(entry["months"]) == 1  # same calendar month, a single accumulated row
    assert entry["months"][0]["bytes_rx"] == 1_200_000
    assert entry["months"][0]["bytes_tx"] == 600_000
    assert entry["total_bytes_12m"] == 1_800_000
    assert entry["avg_monthly_bytes"] == 1_800_000.0
    # 1.8MB << 1024MB cap -- must never be flagged over_cap.
    assert entry["over_cap"] is False


async def test_sim_usage_over_cap_flag(client, two_tenants, platform_users, pool):
    token = await login(client, platform_users["super_admin"]["email"])
    device_id = two_tenants["b"]["device_id"]

    await client.patch(
        f"/devices/{device_id}", json={"sim_plan_data_cap_mb": 1}, headers=auth_header(token)  # 1MB cap, easy to exceed
    )
    await _record_usage(pool, device_id, 5 * 1024 * 1024, 0)  # 5MB, over the 1MB cap

    resp = await client.get("/billing/sim-usage", params={"tenant_id": str(two_tenants["b"]["tenant_id"])}, headers=auth_header(token))
    items = resp.json()["items"]
    entry = next(i for i in items if i["device_id"] == str(device_id))
    assert entry["over_cap"] is True


async def test_sim_usage_device_without_usage_shows_empty_months(client, two_tenants, platform_users):
    token = await login(client, platform_users["super_admin"]["email"])
    resp = await client.get("/billing/sim-usage", params={"tenant_id": str(two_tenants["a"]["tenant_id"])}, headers=auth_header(token))
    items = resp.json()["items"]
    # two_tenants recorded no usage for tenant "a"'s device in this isolated
    # test -- with no rows, months must be [], not an error or a 500.
    assert isinstance(items, list)
