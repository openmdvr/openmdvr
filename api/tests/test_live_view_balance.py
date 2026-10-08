"""GET /devices/{id}/live-view-balance -- the tenant's real live-video
balance (the bridge's central meter) and the cameras open right now."""
from unittest.mock import AsyncMock, patch

import pytest

from conftest import auth_header, login

pytestmark = pytest.mark.asyncio


def _bridge(payload):
    m = AsyncMock()
    m.json = lambda: payload
    return m


async def test_balance_returns_bridge_numbers_for_own_device(client, two_tenants):
    token = await login(client, two_tenants["a"]["email"])
    mock_post = AsyncMock(return_value=_bridge({"code": 0, "remainingSeconds": 540, "activeSessions": 2}))
    with patch("app.routers.video.httpx.AsyncClient") as cls:
        cls.return_value.__aenter__.return_value.post = mock_post
        resp = await client.get(f"/devices/{two_tenants['a']['device_id']}/live-view-balance", headers=auth_header(token))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body == {"tenant_id": str(two_tenants["a"]["tenant_id"]), "remaining_seconds": 540, "active_sessions": 2}
    # The bridge receives the tenant resolved by RLS, never one sent by the client.
    assert mock_post.call_args.kwargs["json"] == {"tenantId": str(two_tenants["a"]["tenant_id"])}


async def test_balance_of_other_tenant_device_is_404_without_calling_bridge(client, two_tenants):
    token = await login(client, two_tenants["a"]["email"])
    with patch("app.routers.video.httpx.AsyncClient") as cls:
        resp = await client.get(f"/devices/{two_tenants['b']['device_id']}/live-view-balance", headers=auth_header(token))
        cls.assert_not_called()
    assert resp.status_code == 404


async def test_balance_bridge_error_is_clean_503(client, two_tenants):
    token = await login(client, two_tenants["a"]["email"])
    mock_post = AsyncMock(return_value=_bridge({"code": 500, "msg": "secret-bridge-detail"}))
    with patch("app.routers.video.httpx.AsyncClient") as cls:
        cls.return_value.__aenter__.return_value.post = mock_post
        resp = await client.get(f"/devices/{two_tenants['a']['device_id']}/live-view-balance", headers=auth_header(token))
    assert resp.status_code == 503
    assert "secret-bridge-detail" not in resp.text
