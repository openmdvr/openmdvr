"""Operational limits come from environment variables (app/limits.py)."""

import pytest

from app import limits


def test_empty_value_uses_default(monkeypatch):
    monkeypatch.setenv("SOME_LIMIT", "")
    assert limits.env_int("SOME_LIMIT", 42) == 42
    monkeypatch.delenv("SOME_LIMIT")
    assert limits.env_int("SOME_LIMIT", 42) == 42


def test_value_overrides_default(monkeypatch):
    monkeypatch.setenv("SOME_LIMIT", " 60000 ")
    assert limits.env_int("SOME_LIMIT", 5000) == 60000
    monkeypatch.setenv("SOME_TIMEOUT", "2.5")
    assert limits.env_float("SOME_TIMEOUT", 10.0) == 2.5


@pytest.mark.parametrize("raw", ["abc", "10#comment", "1.5"])
def test_invalid_value_fails_fast(monkeypatch, raw):
    monkeypatch.setenv("SOME_LIMIT", raw)
    with pytest.raises(RuntimeError, match="SOME_LIMIT"):
        limits.env_int("SOME_LIMIT", 10)


def test_value_below_minimum_fails_fast(monkeypatch):
    monkeypatch.setenv("SOME_LIMIT", "0")
    with pytest.raises(RuntimeError, match=">= 1"):
        limits.env_int("SOME_LIMIT", 10)


def test_routers_use_configured_limits():
    from app.routers import devices, geofences, webhook_endpoints
    from app import webhooks

    assert devices._MAX_ROUTE_POINTS == limits.ROUTE_HISTORY_MAX_POINTS
    assert geofences._MAX_VISITS == limits.GEOFENCE_REPORT_MAX_VISITS
    assert webhook_endpoints._MAX_ENDPOINTS_PER_TENANT == limits.WEBHOOK_MAX_ENDPOINTS_PER_TENANT
    assert webhooks._MAX_ATTEMPTS == limits.WEBHOOK_MAX_ATTEMPTS
