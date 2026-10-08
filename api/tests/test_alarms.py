"""GET /alarms and POST /alarms/{id}/acknowledge -- alarms are detected and
stored by the device servers (JT808 0x0200 alarm bits, GT06 alarm packets).
As everywhere else in this project, tenant isolation is what matters most."""
import uuid

import pytest

from conftest import login, auth_header

pytestmark = pytest.mark.asyncio


async def _insert_alarm(pool, tenant_id, device_id, alarm_type="over_speed", severity="warning"):
    from app import db as db_module

    async with db_module.tenant_scoped_connection(pool, tenant_id=None, bypass=True) as conn:
        row = await (
            await conn.execute(
                "SELECT insert_alarm(%s, %s, now(), %s, %s)",
                (tenant_id, device_id, alarm_type, severity),
            )
        ).fetchone()
        return row[0]


async def test_list_alarms_excludes_other_tenant(client, two_tenants, pool):
    await _insert_alarm(pool, two_tenants["a"]["tenant_id"], two_tenants["a"]["device_id"])
    await _insert_alarm(pool, two_tenants["b"]["tenant_id"], two_tenants["b"]["device_id"])

    token = await login(client, two_tenants["a"]["email"])
    resp = await client.get("/alarms", headers=auth_header(token))
    assert resp.status_code == 200
    device_ids = {a["device_id"] for a in resp.json()}
    assert str(two_tenants["a"]["device_id"]) in device_ids
    assert str(two_tenants["b"]["device_id"]) not in device_ids


async def test_unacknowledged_only_filter(client, two_tenants, pool):
    acked_id = await _insert_alarm(pool, two_tenants["a"]["tenant_id"], two_tenants["a"]["device_id"], "collision_warning", "critical")
    await _insert_alarm(pool, two_tenants["a"]["tenant_id"], two_tenants["a"]["device_id"], "over_speed", "warning")

    token = await login(client, two_tenants["a"]["email"])
    ack_resp = await client.post(f"/alarms/{acked_id}/acknowledge", headers=auth_header(token))
    assert ack_resp.status_code == 204

    resp = await client.get("/alarms?unacknowledged_only=true", headers=auth_header(token))
    assert resp.status_code == 200
    alarm_ids = {a["id"] for a in resp.json()}
    assert str(acked_id) not in alarm_ids


async def test_device_id_filter_returns_only_that_device(client, two_tenants, pool):
    """device_id filters for convenience (per-device preview/history in the
    dashboard) -- alarms_v (security_barrier) remains the real isolation
    barrier; this only tests that the filter works inside the tenant, not that
    it replaces RLS."""
    await _insert_alarm(pool, two_tenants["a"]["tenant_id"], two_tenants["a"]["device_id"], "over_speed", "warning")

    token = await login(client, two_tenants["a"]["email"])
    resp = await client.get(
        "/alarms", params={"device_id": str(two_tenants["a"]["device_id"])}, headers=auth_header(token)
    )
    assert resp.status_code == 200
    device_ids = {a["device_id"] for a in resp.json()}
    assert device_ids == {str(two_tenants["a"]["device_id"])}


async def test_device_id_filter_other_tenant_device_returns_empty(client, two_tenants, pool):
    """Requesting ANOTHER tenant's device_id must not leak through RLS --
    alarms_v already excludes those rows before the filter is applied, so the
    result is simply empty, never a 403/404 that confirms the device exists."""
    await _insert_alarm(pool, two_tenants["b"]["tenant_id"], two_tenants["b"]["device_id"])

    token = await login(client, two_tenants["a"]["email"])
    resp = await client.get(
        "/alarms", params={"device_id": str(two_tenants["b"]["device_id"])}, headers=auth_header(token)
    )
    assert resp.status_code == 200
    assert resp.json() == []


async def test_cannot_acknowledge_other_tenant_alarm(client, two_tenants, pool):
    alarm_id = await _insert_alarm(pool, two_tenants["b"]["tenant_id"], two_tenants["b"]["device_id"])

    token = await login(client, two_tenants["a"]["email"])
    resp = await client.post(f"/alarms/{alarm_id}/acknowledge", headers=auth_header(token))
    assert resp.status_code == 404


async def test_acknowledge_nonexistent_alarm_returns_404(client, two_tenants):
    token = await login(client, two_tenants["a"]["email"])
    resp = await client.post(f"/alarms/{uuid.uuid4()}/acknowledge", headers=auth_header(token))
    assert resp.status_code == 404


async def test_min_severity_filters_informational_alarms(client, two_tenants, pool):
    """Informational alarms (ignition, geofences) must not crowd out the top of
    the list the map uses to flag units "with alarm"."""
    from app import db as db_module
    from conftest import auth_header, login

    a = two_tenants["a"]
    async with db_module.tenant_scoped_connection(pool, tenant_id=None, bypass=True) as conn:
        for _ in range(3):
            await conn.execute("SELECT insert_alarm(%s,%s,now(),'ignition_on','info')", (a["tenant_id"], a["device_id"]))
        await conn.execute("SELECT insert_alarm(%s,%s,now(),'gt06_sos','critical')", (a["tenant_id"], a["device_id"]))
    token = await login(client, a["email"])
    all_rows = (await client.get("/alarms", params={"unacknowledged_only": "true"}, headers=auth_header(token))).json()
    assert len(all_rows) == 4
    rows = (await client.get("/alarms", params={"unacknowledged_only": "true", "min_severity": "warning"}, headers=auth_header(token))).json()
    assert [r["alarm_type"] for r in rows] == ["gt06_sos"]
    assert (await client.get("/alarms", params={"min_severity": "bogus"}, headers=auth_header(token))).status_code == 422
