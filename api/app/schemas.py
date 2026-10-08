"""Pydantic request/response models. Deliberately separate from the database
models (even though they look alike today): an internal schema change must
not automatically leak into the API contract."""
from __future__ import annotations

import datetime as dt
import re
import uuid
from datetime import date
from typing import Generic, Literal, TypeVar

from pydantic import BaseModel, EmailStr, Field, field_validator, model_validator

from .gt06_config_commands import DeviceConfigCommandKey

Role = Literal["super_admin", "support", "tenant_admin", "tenant_operator", "tenant_viewer", "driver"]
TENANT_ROLES = {"tenant_admin", "tenant_operator", "tenant_viewer", "driver"}
PLATFORM_ROLES = {"super_admin", "support"}

# Defined up here (not next to BillingPlanCreate) because TenantOut/
# TenantUpdate need it, and with `from __future__ import annotations`
# Pydantic resolves types by name when building the class -- it must already
# exist in the module before that class.
BillingPeriod = Literal["monthly", "semiannual", "annual"]

# Same reason as BillingPeriod: DeviceCreate/TenantSubscriptionItemCreate need
# it. gt06_video (Jimi IoT JC261/JC400): GT06 telemetry + RTMP video.
DeviceProtocol = Literal["jt808", "gt06", "gt06_video"]

T = TypeVar("T")


# Pagination envelope shared by GET /tenants, /devices, /users -- at the
# target scale (hundreds of tenants, thousands of devices) returning every
# row is not viable. total/limit/offset travel with items so the client can
# render "page X of Y" controls.
class Page(BaseModel, Generic[T]):
    items: list[T]
    total: int
    limit: int
    offset: int


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=200)


class LoginResponse(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"
    role: Role
    # user_id lets the frontend recognize "this Users table row is MY OWN
    # account" (e.g. hide the disable button on oneself) without decoding the
    # JWT client-side -- same approach as tenant_id/role.
    user_id: uuid.UUID
    tenant_id: uuid.UUID | None
    # Tenant branding -- NULL for a platform session (manages several
    # tenants, no single brand) or for a tenant that configured nothing.
    # Sent in the login response, not as a JWT claim: the client never
    # decodes the JWT (see web/src/lib/auth.tsx), it only persists what
    # /auth/login returns.
    tenant_display_name: str | None = None
    tenant_logo_url: str | None = None


class TenantCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)


class TenantOut(BaseModel):
    id: uuid.UUID
    name: str
    status: str
    max_live_view_seconds: int
    # Cumulative MONTHLY quota (consumed by real usage, resets on the 1st of
    # the calendar month, UTC) -- distinct from max_live_view_seconds, which
    # is a per-session cap. See infra/postgres/migrations/
    # 0012_tenant_live_view_quota.sql.
    live_view_monthly_quota_seconds: int
    # Remaining balance = live_view_monthly_quota_seconds - real usage this
    # month (usage_events_v). Computed on each tenants.py query, never
    # persisted, so the dashboard can show it without requesting video first.
    live_view_seconds_remaining: int
    # Tenant self-service branding/policy -- NULL when unset; the UI falls
    # back to defaults.
    display_name: str | None = None
    logo_url: str | None = None
    meal_break_window_start: str | None = None
    meal_break_window_end: str | None = None
    max_shift_hours: float | None = None
    # GPS position retention -- a plan/billing attribute, like
    # max_live_view_seconds: edited by support/platform via
    # PATCH /tenants/{id}, never by the tenant itself.
    gps_retention_days: int = 90
    # Billing cycle -- ALL active tenant_subscription_items lines are
    # invoiced together on this period (generate_invoices(),
    # 0021_billing_invoices.sql).
    billing_period: BillingPeriod = "monthly"
    # Device quota = SUM of quantity over ACTIVE tenant_subscription_items,
    # split by category: a 'camera' plan line covers jt808/gt06_video
    # devices, a 'gps' line covers gt06 devices -- independent quotas, not a
    # shared pool. A custom line (no billing_plan_id) carries its own explicit
    # `category` (see tenant_subscription_items.category, migration 0027).
    # Computed, never persisted, like live_view_seconds_remaining -- lets the
    # UI show "N of M contracted" BEFORE POST /devices returns a 409.
    camera_device_quota: int = 0
    gps_device_quota: int = 0
    # Outgoing webhooks (0035_webhooks.sql) -- approved per tenant by the
    # platform admin: a plan attribute like gps_retention_days, edited ONLY
    # via the bypass-only PATCH /tenants/{id}, never tenant self-service. A
    # tenant_admin can manage their own webhook_endpoints only once this is
    # true.
    webhooks_enabled: bool = False


class TenantUpdate(BaseModel):
    # Partial PATCH on purpose (all fields optional, see tenants.py) -- the
    # dashboard edits them independently.
    #
    # How long a client may watch a live camera per stream session before the
    # video bridge cuts it server-side (adjustable by support/admin, e.g. for
    # a plan with more time).
    max_live_view_seconds: int | None = Field(default=None, ge=5, le=3600)
    # Cumulative monthly live-video quota in seconds -- hard block when
    # exhausted, until next month or until support raises it.
    live_view_monthly_quota_seconds: int | None = Field(default=None, ge=60, le=100_000_000)
    # Days this tenant's GPS positions are kept before
    # enforce_gps_position_retention() (0019_gps_retention.sql) deletes them --
    # a plan attribute, sellable per tier (one week to two years).
    gps_retention_days: int | None = Field(default=None, ge=7, le=730)
    billing_period: BillingPeriod | None = None
    webhooks_enabled: bool | None = None


class TenantSettingsUpdate(BaseModel):
    """Self-service fields a tenant_admin may change on THEIR tenant via
    PATCH /tenants/{id}/settings -- deliberately a separate model from
    TenantUpdate (quota/billing, bypass-only) so this path can never reach
    those fields.

    Unlike TenantUpdate, this is NOT a per-column partial PATCH: the body
    replaces all 5 fields on every request (the UI already has the current
    state). This avoids the "does None mean 'leave as is' or 'set NULL'?"
    ambiguity, which matters here because 'no meal window'/'no max hours'
    are valid NULL states."""
    display_name: str | None = Field(default=None, max_length=200)
    logo_url: str | None = Field(default=None, max_length=2000)
    meal_break_window_start: str | None = Field(default=None, pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    meal_break_window_end: str | None = Field(default=None, pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    max_shift_hours: float | None = Field(default=None, ge=0.5, le=48)

    @field_validator("logo_url")
    @classmethod
    def _logo_url_scheme(cls, v: str | None) -> str | None:
        # Security review finding: without this, a tenant_admin could store
        # `javascript:...` (harmless in <img src>, but real XSS the day this
        # value is used in an <a href> or dangerouslySetInnerHTML) or any
        # other unintended scheme, rendered as-is in the rail/header of EVERY
        # user of that tenant, including drivers' phones.
        if v and not (v.startswith("https://") or v.startswith("http://")):
            raise ValueError("logo_url must start with http:// or https://")
        return v

    @field_validator("meal_break_window_end")
    @classmethod
    def _meal_window_both_or_neither(cls, v: str | None, info) -> str | None:
        start = info.data.get("meal_break_window_start")
        if (v is None) != (start is None):
            raise ValueError("meal_break_window_start and meal_break_window_end must be provided together or not at all")
        return v


class UserCreate(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=200)
    role: Role
    # Required for tenant roles, must be None for platform roles -- validated
    # explicitly in the router (see users.py) because the rule depends on the
    # VALUE of `role`.
    tenant_id: uuid.UUID | None = None
    # Required ONLY when role="driver" (links the login to its `drivers`
    # row), None for any other role -- same value-dependent rule as tenant_id.
    driver_id: uuid.UUID | None = None


class UserOut(BaseModel):
    id: uuid.UUID
    email: str
    role: Role
    tenant_id: uuid.UUID | None
    driver_id: uuid.UUID | None = None
    status: str
    status_changed_by: uuid.UUID | None = None
    status_changed_at: str | None = None


# Dedicated endpoint (not a general user PATCH, which does not exist) --
# enabling/disabling is the only edit supported.
class UserStatusUpdate(BaseModel):
    status: Literal["active", "disabled"]


# Admin password reset. The admin chooses the new password directly (same
# validation as UserCreate.password) rather than a one-time email link --
# there is no outgoing email service yet.
class UserPasswordReset(BaseModel):
    new_password: str = Field(min_length=8, max_length=200)


# jt808_terminal_id: digits only, no leading zero -- same reason as the CHECK
# in infra/postgres/migrations/0006_devices.sql (the JT808 server's BCD
# decoder strips them; storing one with a leading zero would mean the real
# device could never authenticate). Validating here gives the operator a
# clear error instead of a 500 from the CHECK violation.
_TERMINAL_ID_RE = re.compile(r"^[1-9][0-9]*$")


# GT06 IMEI: exactly 15 digits. Unlike jt808_terminal_id there is no
# leading-zero gotcha (see migration 0026 -- the IMEI is a direct hex dump of
# the login packet, not stripped BCD).
_GT06_IMEI_RE = re.compile(r"^[0-9]{15}$")


# --- Device model catalog (organizational) ---
class DeviceModelCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    protocol: DeviceProtocol
    notes: str | None = Field(default=None, max_length=2000)


class DeviceModelOut(BaseModel):
    id: uuid.UUID
    name: str
    protocol: DeviceProtocol
    notes: str | None
    created_at: str


class DeviceCreate(BaseModel):
    tenant_id: uuid.UUID
    protocol: DeviceProtocol = "jt808"
    # Exactly one of these must be present, depending on `protocol` --
    # validated in _identifier_matches_protocol below (mirrors the
    # devices_protocol_identifier_match CHECK in migration 0026).
    jt808_terminal_id: str | None = Field(default=None, min_length=1, max_length=30)
    gt06_imei: str | None = Field(default=None, min_length=15, max_length=15)
    label: str = Field(min_length=1, max_length=200)
    # The vehicle is assigned separately (POST /vehicles, then this id) -- see
    # docs/architecture.md. Never another tenant's vehicle:
    # enforce_vehicle_tenant_match() in migration 0014 rejects it at the
    # schema level, not only here.
    vehicle_id: uuid.UUID | None = None
    notes: str | None = Field(default=None, max_length=2000)
    # Model (device_models catalog) + SIM -- both nullable.
    # sim_plan_cost_mxn_month/sim_plan_data_cap_mb are deliberately NOT set
    # here -- they are configured later via PATCH (a plan attribute adjusted
    # after physical installation, not at creation).
    device_model_id: uuid.UUID | None = None
    sim_number: str | None = Field(default=None, max_length=30)
    sim_carrier: str | None = Field(default=None, max_length=60)

    @field_validator("jt808_terminal_id")
    @classmethod
    def _no_leading_zero(cls, v: str | None) -> str | None:
        if v is not None and not _TERMINAL_ID_RE.match(v):
            raise ValueError(
                "jt808_terminal_id must contain digits only, without a leading zero "
                "(the JT808 server's BCD decoder strips them)"
            )
        return v

    @field_validator("gt06_imei")
    @classmethod
    def _imei_shape(cls, v: str | None) -> str | None:
        if v is not None and not _GT06_IMEI_RE.match(v):
            raise ValueError("gt06_imei must be exactly 15 digits")
        return v

    # model_validator (not field_validator) on purpose: a field_validator on
    # gt06_imei does NOT run when the client omits the field entirely
    # (Pydantic v2 skips validators on defaults unless validate_default=True),
    # so protocol='gt06' with jt808_terminal_id set and gt06_imei ABSENT would
    # slip through. model_validator(mode="after") always runs -- a clear 422
    # instead of hitting the quota or the DB CHECK
    # (devices_protocol_identifier_match, migration 0026) with a less clear
    # error.
    @model_validator(mode="after")
    def _identifier_matches_protocol(self) -> "DeviceCreate":
        # gt06_video (Jimi IoT JC261/JC400) shares its identifier with plain
        # gt06 (same IMEI, same CHECK branch in migration 0039). Without this
        # branch, protocol='gt06_video' would pass unchecked and the clean 422
        # would become a raw 500 from the Postgres CHECK.
        if self.protocol in ("gt06", "gt06_video"):
            if self.gt06_imei is None:
                raise ValueError(f"gt06_imei is required for protocol='{self.protocol}'")
            if self.jt808_terminal_id is not None:
                raise ValueError(f"jt808_terminal_id must be None for protocol='{self.protocol}'")
        elif self.protocol == "jt808":
            if self.gt06_imei is not None:
                raise ValueError("gt06_imei must be None for protocol='jt808'")
            if self.jt808_terminal_id is None:
                raise ValueError("jt808_terminal_id is required for protocol='jt808'")
        return self


# Partial PATCH (all optional, same COALESCE pattern as TenantUpdate) --
# tenant_id and jt808_terminal_id/gt06_imei are NOT editable on purpose:
# moving a device to another tenant or changing its real connection
# identifier is "reinstalling hardware", not a data fix -- handled as
# decommission + new device, never a silent edit.
class DeviceUpdate(BaseModel):
    label: str | None = Field(default=None, min_length=1, max_length=200)
    vehicle_id: uuid.UUID | None = None
    notes: str | None = Field(default=None, max_length=2000)
    # Soft delete/deactivation -- never a real DELETE (RESTRICT FKs from
    # gps_positions/alarms/usage_events would prevent it anyway).
    # 'inactive'/'maintenance' have real effect (video.py/device_commands.py
    # require status='active').
    status: Literal["active", "inactive", "maintenance"] | None = None
    # Deliberate exception to "the identifier is not touched": gt06_imei stays
    # the same, only WHICH protocol interprets it changes -- an IMEI
    # registered as 'gt06' (GPS-only) that is actually a camera (or vice
    # versa) is fixed here without "reinstalling hardware": same physical
    # device, same TCP connection, only its capabilities change. Never
    # includes 'jt808' -- that requires a different identifier column, so it
    # remains decommission + new device. See update_device() for the target
    # category quota check.
    protocol: Literal["gt06", "gt06_video"] | None = None
    device_model_id: uuid.UUID | None = None
    sim_number: str | None = Field(default=None, max_length=30)
    sim_carrier: str | None = Field(default=None, max_length=60)
    # Contracted SIM line cost/cap -- PLATFORM-ONLY (never in DeviceOut, only
    # read back via GET /billing/sim-usage, see billing.py -- cost data never
    # lives in a schema shared with tenant sessions). Bound calibrated to the
    # column's real NUMERIC(10,2) (0045), like _MAX_COST_SETTING -- a mismatch
    # there would surface as a raw 500.
    sim_plan_cost_mxn_month: float | None = Field(default=None, ge=0, le=99_999_999.99)
    sim_plan_data_cap_mb: int | None = Field(default=None, ge=0)


class DeviceOut(BaseModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    protocol: DeviceProtocol
    jt808_terminal_id: str | None
    gt06_imei: str | None
    label: str
    device_model_id: uuid.UUID | None
    device_model_name: str | None
    # Visible to any session that can already see the device (including
    # tenant_admin) -- unlike sim_plan_cost_mxn_month/sim_plan_data_cap_mb,
    # which never appear here (see DeviceUpdate).
    sim_number: str | None
    sim_carrier: str | None
    vehicle_id: uuid.UUID | None
    notes: str | None
    status: str
    last_seen_at: str | None
    status_changed_by: uuid.UUID | None = None
    status_changed_at: str | None = None
    # Telemetry reported by the device itself -- NEVER editable via
    # PATCH /devices/{id} (not in DeviceUpdate), like last_seen_at. NULL = not
    # reported yet (see migration 0046 and jt808server/gt06server for the
    # source of each value).
    ignition_on: bool | None = None
    ignition_changed_at: str | None = None
    power_connected: bool | None = None
    power_changed_at: str | None = None


class VehicleCreate(BaseModel):
    tenant_id: uuid.UUID
    plate: str | None = Field(default=None, max_length=20)
    make: str | None = Field(default=None, max_length=100)
    model: str | None = Field(default=None, max_length=100)
    year: int | None = Field(default=None, ge=1980, le=2100)
    notes: str | None = Field(default=None, max_length=2000)
    # See 0050_vehicle_max_speed.sql -- NULL = no limit configured. The
    # generous upper bound (500) only rejects obvious garbage, it is not a
    # business limit.
    max_speed_kmh: float | None = Field(default=None, gt=0, le=500)


class VehicleUpdate(BaseModel):
    plate: str | None = Field(default=None, max_length=20)
    make: str | None = Field(default=None, max_length=100)
    model: str | None = Field(default=None, max_length=100)
    year: int | None = Field(default=None, ge=1980, le=2100)
    notes: str | None = Field(default=None, max_length=2000)
    max_speed_kmh: float | None = Field(default=None, gt=0, le=500)


class VehicleOut(BaseModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    plate: str | None
    make: str | None
    model: str | None
    year: int | None
    status: str
    notes: str | None
    max_speed_kmh: float | None = None
    # Driver with the active assignment (ended_at IS NULL), if any -- built
    # with a LEFT JOIN in the router, not a separate call.
    current_driver_id: uuid.UUID | None = None
    current_driver_name: str | None = None


class DriverCreate(BaseModel):
    tenant_id: uuid.UUID
    name: str = Field(min_length=1, max_length=200)
    license_number: str | None = Field(default=None, max_length=50)
    phone: str | None = Field(default=None, max_length=50)
    notes: str | None = Field(default=None, max_length=2000)


class DriverUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    license_number: str | None = Field(default=None, max_length=50)
    phone: str | None = Field(default=None, max_length=50)
    notes: str | None = Field(default=None, max_length=2000)


class DriverOut(BaseModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    name: str
    license_number: str | None
    phone: str | None
    status: str
    notes: str | None
    # Vehicle with the active assignment, if any -- same as
    # VehicleOut.current_driver_*.
    current_vehicle_id: uuid.UUID | None = None
    current_vehicle_plate: str | None = None


class AssignDriverRequest(BaseModel):
    driver_id: uuid.UUID


# --- Device groups + user<->device assignment (alert routing) ---
# See infra/postgres/migrations/0031_device_groups_and_assignments.sql --
# tenant_admin always sees the whole tenant (no assignment row needed);
# these tables only matter for tenant_operator/tenant_viewer.

class DeviceGroupCreate(BaseModel):
    tenant_id: uuid.UUID
    name: str = Field(min_length=1, max_length=200)


class DeviceGroupUpdate(BaseModel):
    name: str = Field(min_length=1, max_length=200)


class DeviceGroupOut(BaseModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    name: str
    device_count: int = 0


# Replaces the group's FULL member set (like TenantSettingsUpdate: the client
# already has the current state -- simpler than incremental add/remove).
class DeviceGroupMembersUpdate(BaseModel):
    device_ids: list[uuid.UUID] = Field(default_factory=list)


# Replaces a user's FULL direct + group assignment -- same full-replacement
# approach as DeviceGroupMembersUpdate.
class UserDeviceAssignmentsUpdate(BaseModel):
    device_ids: list[uuid.UUID] = Field(default_factory=list)
    device_group_ids: list[uuid.UUID] = Field(default_factory=list)


class UserDeviceAssignmentsOut(BaseModel):
    device_ids: list[uuid.UUID]
    device_group_ids: list[uuid.UUID]


class UserNotificationSettingsOut(BaseModel):
    in_app_enabled: bool = True
    email_enabled: bool = False


class UserNotificationSettingsUpdate(BaseModel):
    in_app_enabled: bool | None = None
    email_enabled: bool | None = None


RouteStatus = Literal["planned", "in_progress", "completed", "cancelled"]


class RouteCreate(BaseModel):
    tenant_id: uuid.UUID
    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    # dt.date (not str): security review finding -- an unvalidated string
    # reached Postgres raw and any unparseable value (e.g. "zzz") produced a
    # 500 instead of a clean 422.
    # Aliased as `dt.date` (not the `date` imported above) because the field
    # has the same name as the type -- with `from __future__ import
    # annotations` Pydantic would resolve "date" against the class attribute
    # itself instead of the imported type (NoneType instead of datetime.date).
    date: dt.date
    driver_id: uuid.UUID | None = None
    vehicle_id: uuid.UUID | None = None


class RouteUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    date: dt.date | None = None
    driver_id: uuid.UUID | None = None
    vehicle_id: uuid.UUID | None = None
    status: RouteStatus | None = None


class RouteOut(BaseModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    name: str
    description: str | None
    date: str
    driver_id: uuid.UUID | None
    driver_name: str | None
    vehicle_id: uuid.UUID | None
    vehicle_plate: str | None
    status: RouteStatus


class DistanceDay(BaseModel):
    date: str
    distance_km: float
    position_count: int


class VehicleDistanceReport(BaseModel):
    vehicle_id: uuid.UUID
    # None if the vehicle has no installed device today -- empty report, not
    # an error (see vehicles.py: the report uses the CURRENTLY linked device,
    # it does not reconstruct reinstallation history).
    device_id: uuid.UUID | None
    date_from: str
    date_to: str
    days: list[DistanceDay]
    total_distance_km: float


class EngineHoursDay(BaseModel):
    date: str
    driving_hours: float
    idle_hours: float
    engine_off_hours: float


class VehicleEngineHoursReport(BaseModel):
    vehicle_id: uuid.UUID
    device_id: uuid.UUID | None
    date_from: str
    date_to: str
    # Only counts from when ignition_on/ignition_off alarms started being
    # recorded -- no attempt to reconstruct earlier history.
    days: list[EngineHoursDay]
    total_driving_hours: float
    total_idle_hours: float
    total_engine_off_hours: float


ShiftEventType = Literal["clock_in", "clock_out", "meal_start", "meal_end"]


class ShiftEventCreate(BaseModel):
    event_type: ShiftEventType
    lat: float | None = Field(default=None, ge=-90, le=90)
    lon: float | None = Field(default=None, ge=-180, le=180)


class ShiftEventOut(BaseModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    driver_id: uuid.UUID
    driver_name: str
    event_type: ShiftEventType
    occurred_at: str
    lat: float | None
    lon: float | None
    source: Literal["driver_app", "manual_admin"]


class DriverShiftStatusOut(BaseModel):
    """One row per tenant driver with their latest shift event -- backs the
    "On shift now" card in Operations.tsx. None when the driver never
    recorded an event. last_lat/last_lon are the best-effort coordinates
    (may be None) captured by DriverHome.tsx at THAT event, so the map can
    show where the driver clocked in."""
    driver_id: uuid.UUID
    driver_name: str
    last_event_type: ShiftEventType | None
    last_event_at: str | None
    last_lat: float | None = None
    last_lon: float | None = None


class WorkedDay(BaseModel):
    date: str
    hours_worked: float
    completed_shifts: int


class DriverHoursReport(BaseModel):
    driver_id: uuid.UUID
    date_from: str
    date_to: str
    days: list[WorkedDay]
    total_hours: float


DriverShiftAlertType = Literal["meal_outside_window", "shift_exceeds_max_hours"]


class DriverShiftAlertOut(BaseModel):
    id: uuid.UUID
    driver_id: uuid.UUID
    driver_name: str
    alert_type: DriverShiftAlertType
    details: dict | None
    occurred_at: str
    acknowledged_at: str | None


class VideoRequest(BaseModel):
    channel: int = Field(default=1, ge=0, le=255)


class LiveViewBalance(BaseModel):
    """Tenant live-video balance (seconds) and currently open cameras -- the
    dashboard decrements it locally by `active_sessions` seconds per second
    between polls."""

    tenant_id: uuid.UUID
    remaining_seconds: int
    active_sessions: int


class VideoResponse(BaseModel):
    # HTTP-FLV playback URL WITH the one-time ticket already in the query
    # string (?token=...). The frontend uses it as-is -- the bridge minted the
    # ticket (via the API) after checking permission, and ZLMediaKit validates
    # it in its on_play hook before serving a single byte. Without a valid
    # token: clean 401 and the camera is never even turned on.
    #
    # Legacy -- to be removed once the WebRTC path is fully confirmed; the
    # frontend already uses webrtc_url exclusively.
    url: str
    # WHEP signaling URL (POST with the SDP offer as body) WITH the SAME
    # one-time ticket included (?app=&stream=&token=) -- same authorization
    # mechanism as `url` (ZLMediaKit's on_play fires for WebRTC too). The
    # frontend POSTs the SDP offer to this URL as a plain-text body.
    webrtc_url: str
    # Seconds until the bridge cuts the stream server-side
    # (tenants.max_live_view_seconds) -- informational, for a client
    # countdown; the real cut does not depend on it.
    expires_in_seconds: int
    # Remaining MONTHLY tenant quota (live_view_monthly_quota_seconds - real
    # usage this month) -- also informational; the real block happens in the
    # bridge BEFORE this response (see 402).
    live_view_seconds_remaining: int = 0


class PositionStreamTicket(BaseModel):
    """Opaque one-time token for GET /positions/stream -- EventSource cannot
    send the Authorization header, so the regular JWT never travels in the
    stream URL (see api/app/live_positions.py)."""
    ticket: str
    expires_in: int


class DevicePosition(BaseModel):
    device_id: uuid.UUID
    label: str
    lat: float
    lon: float
    speed_kmh: float | None
    heading: float | None
    time: str


AlarmSeverity = Literal["info", "warning", "critical"]


# --- Route history -- see api/app/routers/devices.py::device_route_history.


class RouteHistoryPoint(BaseModel):
    time: str
    lat: float
    lon: float
    speed_kmh: float | None
    heading: float | None


class RouteHistoryEvent(BaseModel):
    id: uuid.UUID
    time: str
    alarm_type: str
    severity: AlarmSeverity
    # None if no GPS position was found within ±5 min of the alarm (device
    # just connected, signal gap) -- the pin is not drawn on the map, but the
    # event still appears in the list.
    lat: float | None
    lon: float | None
    has_video_clip: bool


class RouteHistoryReport(BaseModel):
    device_id: uuid.UUID
    date_from: str
    date_to: str
    points: list[RouteHistoryPoint]
    # Effective resolution -- each point represents up to this many seconds
    # of real track, compressed by time_bucket() in the database.
    # Informational for the frontend ("resolution: ~Ns"), never an error: the
    # point count is ALWAYS bounded by max_points regardless of how many raw
    # positions exist in the requested window.
    bucket_seconds: float
    events: list[RouteHistoryEvent]
    # true if there were more than max_events alarms in the window. Unlike
    # points (bounded by design via time_bucket), each alarm is a real
    # discrete event that cannot be "compressed" without losing it, so an
    # excess IS truncated and flagged explicitly instead of silently
    # returning an incomplete list.
    events_truncated: bool


class AlarmOut(BaseModel):
    id: uuid.UUID
    device_id: uuid.UUID
    device_label: str
    alarm_type: str
    severity: AlarmSeverity
    time: str
    acknowledged_at: str | None
    # has_video_clip: true if alarms.video_evidence_key is set (a clip request
    # reached 'ready') -- the raw storage key is never exposed, only the
    # boolean; the signed URL is requested via GET /alarms/{id}/clip (see
    # AlarmVideoClipOut).
    has_video_clip: bool


# --- Alarm-linked video clip retrieval. AlarmVideoClipStatus mirrors the
# alarm_video_clips.status CHECK (0040_alarm_video_clips.sql).
AlarmVideoClipStatus = Literal["requested", "uploading", "ready", "failed", "unsupported"]


class AlarmVideoClipOut(BaseModel):
    id: uuid.UUID
    alarm_id: uuid.UUID
    status: AlarmVideoClipStatus
    requested_at: str
    completed_at: str | None
    error_detail: str | None
    # url: only set when status == 'ready' -- short-lived signed read URL
    # (storage.generate_signed_url), never the raw storage_key.
    url: str | None
    # url_secondary: the cabin camera, when the device recorded both
    # (migration 0044). It may arrive AFTER status is already 'ready'
    # (best-effort, chained after the front camera, see alarmclip.go) -- so
    # NULL here is not an error and does not mean it will never arrive.
    url_secondary: str | None


# --- Notifications (alerts per tenant/user/device) ---
# See infra/postgres/migrations/0033_notifications.sql -- event_type is free
# text on purpose (today only 'device_alarm') so future emitters
# (driver_shift_alert/device_offline/billing_overdue) can join the SAME
# pipeline without a new migration.

class NotificationOut(BaseModel):
    id: uuid.UUID
    event_type: str
    device_id: uuid.UUID | None
    alarm_id: uuid.UUID | None
    title: str
    body: str | None
    severity: AlarmSeverity | None
    created_at: str
    read_at: str | None


class NotificationListOut(BaseModel):
    items: list[NotificationOut]
    total: int
    # Counted separately from the paginated total -- the bell/badge needs the
    # real unread count regardless of which page the client is showing.
    unread_count: int
    limit: int
    offset: int


class NotificationStreamTicket(BaseModel):
    """Same mechanism as PositionStreamTicket -- EventSource cannot send the
    Authorization header (see api/app/notifications.py)."""
    ticket: str
    expires_in: int


# ---------------------------------------------------------------------------
# API keys (0034_api_keys.sql) -- M2M integrations that authenticate AS an
# existing user (same role/tenant/device assignment), restricted by
# can_write (read-only vs. read-write umbrella) and optionally by
# allowed_device_ids (narrows FURTHER, within what the user can already see).
# See api/app/deps.py::get_current_user and api/app/api_key_auth.py for the
# actual enforcement.
# ---------------------------------------------------------------------------
class ApiKeyCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    can_write: bool = False
    # None = everything the user can already see, no extra narrowing.
    allowed_device_ids: list[uuid.UUID] | None = None
    # Required by the column (NOT NULL) -- explicit default here (1 year) so
    # the creation form need not compute a date, but never "forever": the
    # maximum (2 years) still forces periodic rotation.
    expires_in_days: int = Field(default=365, ge=1, le=730)


class ApiKeyOut(BaseModel):
    id: uuid.UUID
    name: str
    key_prefix: str
    can_write: bool
    allowed_device_ids: list[uuid.UUID] | None
    created_at: str
    expires_at: str
    revoked_at: str | None
    last_used_at: str | None
    # Who created/revoked it (security review finding) -- a tenant_admin must
    # be able to see that a key in THEIR panel was issued by a platform
    # session, not by them. None if that account was later deleted
    # (ON DELETE SET NULL -- the key survives, only the trace is lost).
    created_by: uuid.UUID | None
    revoked_by: uuid.UUID | None


class ApiKeyCreatedOut(ApiKeyOut):
    """The only time the full key travels through the API -- never again
    after this, not even for a super_admin (see the key_hash comment in the
    migration)."""
    raw_key: str


class ApiKeyUsageOut(BaseModel):
    occurred_at: str
    method: str
    path: str
    status_code: int
    ip_address: str | None


# ---------------------------------------------------------------------------
# Outgoing webhooks (0035_webhooks.sql) -- the "push" counterpart of API keys
# ("pull"): instead of a third party polling, this server notifies a URL the
# third party controls. Requires per-tenant platform approval
# (tenants.webhooks_enabled above) -- NEVER self-service without it, even
# though the tenant_admin manages their own endpoints once enabled.
#
# WEBHOOK_EVENT_TYPES is an explicit set (not a fixed Literal) on purpose:
# adding a new event type (driver_shift_alert, device_offline,
# billing_overdue -- the same set notifications.py anticipates) is ONE line
# here, not a schema change.
# ---------------------------------------------------------------------------
WEBHOOK_EVENT_TYPES = {"device_alarm"}


class WebhookEndpointCreate(BaseModel):
    tenant_id: uuid.UUID
    url: str = Field(min_length=1, max_length=2000)
    event_types: list[str] = Field(min_length=1, max_length=len(WEBHOOK_EVENT_TYPES))
    enabled: bool = True

    @field_validator("url")
    @classmethod
    def _url_scheme(cls, v: str) -> str:
        if not (v.startswith("https://") or v.startswith("http://")):
            raise ValueError("url must start with http:// or https://")
        return v

    @field_validator("event_types")
    @classmethod
    def _known_event_types(cls, v: list[str]) -> list[str]:
        unknown = sorted(set(v) - WEBHOOK_EVENT_TYPES)
        if unknown:
            raise ValueError(f"unknown event type(s): {unknown} -- valid: {sorted(WEBHOOK_EVENT_TYPES)}")
        return list(dict.fromkeys(v))  # dedupe preserving order


class WebhookEndpointUpdate(BaseModel):
    url: str | None = Field(default=None, min_length=1, max_length=2000)
    event_types: list[str] | None = Field(default=None, min_length=1, max_length=len(WEBHOOK_EVENT_TYPES))
    enabled: bool | None = None

    @field_validator("url")
    @classmethod
    def _url_scheme(cls, v: str | None) -> str | None:
        if v is not None and not (v.startswith("https://") or v.startswith("http://")):
            raise ValueError("url must start with http:// or https://")
        return v

    @field_validator("event_types")
    @classmethod
    def _known_event_types(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return v
        unknown = sorted(set(v) - WEBHOOK_EVENT_TYPES)
        if unknown:
            raise ValueError(f"unknown event type(s): {unknown} -- valid: {sorted(WEBHOOK_EVENT_TYPES)}")
        return list(dict.fromkeys(v))


class WebhookEndpointOut(BaseModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    url: str
    event_types: list[str]
    enabled: bool
    consecutive_failures: int
    disabled_at: str | None
    disabled_reason: str | None
    last_attempt_at: str | None
    last_success_at: str | None
    created_by: uuid.UUID | None
    created_at: str


class WebhookEndpointCreatedOut(WebhookEndpointOut):
    """The only time the signing secret travels through the API -- see the
    comment in 0035_webhooks.sql on why this (unlike api_keys.key_hash) IS
    stored in a reversible form."""
    secret: str


class WebhookTestOut(BaseModel):
    """Result of POST /webhook-endpoints/{id}/test -- synchronous delivery of
    a signed `ping` event so the tenant_admin can immediately see whether
    their receiver works (HTTP status, latency and the real error)."""
    success: bool
    status_code: int | None
    error: str | None
    elapsed_ms: int


class WebhookDeliveryOut(BaseModel):
    id: uuid.UUID
    event_type: str
    status: str
    attempt_count: int
    next_attempt_at: str
    response_status_code: int | None
    last_error: str | None
    created_at: str
    delivered_at: str | None


# ---------------------------------------------------------------------------
# Billing -- plan catalog + per-tenant subscriptions. billing_plans is a
# GLOBAL catalog, never readable by a tenant_admin (see
# 0020_billing_catalog.sql) -- BillingPlanOut/Create/Update only travel on
# bypass-only endpoints.
# ---------------------------------------------------------------------------
BillingPlanCategory = Literal["gps", "camera", "addon"]

# Real bound of the NUMERIC(12, 2) column in 0020_billing_catalog.sql --
# without it an extreme value passes Pydantic and blows up as an uncaught
# Postgres exception (raw 500) instead of a clean 422.
_MAX_UNIT_PRICE = 9_999_999_999.99


class BillingPlanCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    sku: str = Field(min_length=1, max_length=100)
    category: BillingPlanCategory
    unit_price: float = Field(ge=0, le=_MAX_UNIT_PRICE)
    currency: str = Field(default="MXN", min_length=3, max_length=3)
    billing_period: BillingPeriod = "monthly"


class BillingPlanUpdate(BaseModel):
    """Partial PATCH -- sku/category are not editable on purpose (plan
    identity; a real category change is a new plan, not an edit, so the
    history of what was sold as what stays unambiguous)."""
    name: str | None = Field(default=None, min_length=1, max_length=200)
    unit_price: float | None = Field(default=None, ge=0, le=_MAX_UNIT_PRICE)
    billing_period: BillingPeriod | None = None
    active: bool | None = None


class BillingPlanOut(BaseModel):
    id: uuid.UUID
    name: str
    sku: str
    category: BillingPlanCategory
    unit_price: float
    currency: str
    billing_period: BillingPeriod
    active: bool


class TenantSubscriptionItemCreate(BaseModel):
    tenant_id: uuid.UUID
    billing_plan_id: uuid.UUID | None = None
    custom_description: str | None = Field(default=None, max_length=500)
    # Category of THIS line when there is no billing_plan_id (a line with a
    # plan inherits bp.category via join and this field is ignored). Device
    # quota is computed per category (camera=jt808/gt06_video, gps=gt06), so
    # a custom line without a category could not count toward any quota.
    # Validated below: required when billing_plan_id is None, like
    # custom_description.
    category: BillingPlanCategory | None = None
    # Sanity cap (the largest fleets contemplated are thousands of units, not
    # hundreds of thousands) -- security review finding: without it,
    # quantity * unit_price can overflow NUMERIC(12,2) inside
    # generate_invoices(). Not the only defense (see also the per-tenant
    # EXCEPTION block in 0021_billing_invoices.sql), but it closes the most
    # direct path.
    quantity: int = Field(default=1, ge=1, le=100_000)
    # Price different from the plan's list price for THIS tenant -- lets the
    # platform charge per tenant without forking the catalog. None = use the
    # plan's list price.
    unit_price_override: float | None = Field(default=None, ge=0, le=_MAX_UNIT_PRICE)

    # model_validator (not field_validator) on purpose: a field_validator on
    # `category` does NOT run when the client omits the field entirely
    # (Pydantic v2 skips validators on defaults unless validate_default=True)
    # -- same gap as DeviceCreate._identifier_matches_protocol, same fix.
    @model_validator(mode="after")
    def _category_required_without_plan(self) -> "TenantSubscriptionItemCreate":
        if self.billing_plan_id is None and self.category is None:
            raise ValueError("category is required when billing_plan_id is not set")
        return self


class TenantSubscriptionItemUpdate(BaseModel):
    quantity: int | None = Field(default=None, ge=1, le=100_000)
    unit_price_override: float | None = Field(default=None, ge=0, le=_MAX_UNIT_PRICE)
    # True = end the line now (set ended_at = now()). Lines are never deleted,
    # only ended -- history of what a tenant had contracted and when.
    end_now: bool = False


class TenantSubscriptionItemOut(BaseModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    billing_plan_id: uuid.UUID | None
    plan_name: str | None
    plan_sku: str | None
    custom_description: str | None
    quantity: int
    unit_price_override: float | None
    # unit_price_override if set, otherwise the plan's list price -- the
    # number actually charged to this tenant per unit.
    effective_unit_price: float
    started_at: str
    ended_at: str | None


# ---------------------------------------------------------------------------
# Billing -- promotions + invoices (see 0021_billing_invoices.sql for the
# full RLS rationale and the generate_invoices job). invoices IS readable by
# the tenant itself via RLS (unlike billing_plans/tenant_promotions) -- it
# backs the tenant's "My billing" view.
# ---------------------------------------------------------------------------
PromotionDiscountType = Literal["full_waiver", "percentage", "fixed_amount"]
InvoiceStatus = Literal["draft", "issued", "paid", "overdue", "void"]


class TenantPromotionCreate(BaseModel):
    tenant_id: uuid.UUID
    description: str = Field(min_length=1, max_length=300)
    starts_at: date
    ends_at: date
    discount_type: PromotionDiscountType
    # percentage: 0-100 (validated below). fixed_amount: amount in the
    # tenant's currency, capped at the actual subtotal when applied.
    # full_waiver: ignored, the discount is always 100% of the subtotal.
    discount_value: float = Field(default=0, ge=0, le=_MAX_UNIT_PRICE)

    @field_validator("ends_at")
    @classmethod
    def _ends_not_before_starts(cls, v: date, info) -> date:
        starts = info.data.get("starts_at")
        if starts is not None and v < starts:
            raise ValueError("ends_at cannot be earlier than starts_at")
        return v

    @field_validator("discount_value")
    @classmethod
    def _percentage_capped_at_100(cls, v: float, info) -> float:
        if info.data.get("discount_type") == "percentage" and v > 100:
            raise ValueError("a percentage discount_value cannot exceed 100")
        return v


class TenantPromotionOut(BaseModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    description: str
    starts_at: str
    ends_at: str
    discount_type: PromotionDiscountType
    discount_value: float


class InvoiceLineItemOut(BaseModel):
    id: uuid.UUID
    description: str
    quantity: int
    unit_price: float
    subtotal: float


class InvoiceOut(BaseModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    period_start: str
    period_end: str
    issued_at: str
    due_date: str
    subtotal: float
    discount_total: float
    total: float
    currency: str
    status: InvoiceStatus
    line_items: list[InvoiceLineItemOut] = []


# ---------------------------------------------------------------------------
# Billing -- payments (see 0022_billing_payments.sql and
# api/app/payments.py::PaymentProvider for the full rationale).
# ---------------------------------------------------------------------------
PaymentMethod = Literal["cash", "bank_transfer", "stripe", "mercado_pago", "other"]


class PaymentCreate(BaseModel):
    invoice_id: uuid.UUID
    amount: float = Field(gt=0, le=_MAX_UNIT_PRICE)
    method: PaymentMethod
    reference_note: str | None = Field(default=None, max_length=500)


class PaymentOut(BaseModel):
    id: uuid.UUID
    invoice_id: uuid.UUID
    tenant_id: uuid.UUID
    amount: float
    method: PaymentMethod
    received_at: str
    recorded_by: uuid.UUID | None
    reference_note: str | None
    external_provider: str | None
    external_payment_id: str | None
    # Invoice status AFTER this payment -- a frontend convenience (avoids a
    # second GET /billing/invoices/{id} after recording).
    invoice_status: InvoiceStatus


# ---------------------------------------------------------------------------
# Billing -- estimated cost and margin (see 0023_billing_cost_estimation.sql).
# Never reachable by a tenant_admin: neither the cost assumptions nor the
# profitability report.
# ---------------------------------------------------------------------------
class PlatformBillingSettingsOut(BaseModel):
    cost_usd_per_device_month: float
    # Separate rate for a gt06 device (GPS-only, no video) -- see
    # 0027_billing_category_quota.sql.
    cost_usd_per_gps_device_month: float
    cost_usd_per_gb: float
    exchange_rate_mxn_per_usd: float
    updated_at: str


# Real bound of these three NUMERIC(10, 4) columns -- deliberately NOT
# _MAX_UNIT_PRICE (calibrated for NUMERIC(12, 2), almost four orders of
# magnitude larger). Reusing that bound here was a security review finding: a
# value as modest as 1,000,000 passed Pydantic but blew up the UPDATE with a
# raw 500 (uncaught NumericValueOutOfRange) instead of a clean 422.
_MAX_COST_SETTING = 999_999.9999


class PlatformBillingSettingsUpdate(BaseModel):
    cost_usd_per_device_month: float | None = Field(default=None, ge=0, le=_MAX_COST_SETTING)
    cost_usd_per_gps_device_month: float | None = Field(default=None, ge=0, le=_MAX_COST_SETTING)
    cost_usd_per_gb: float | None = Field(default=None, ge=0, le=_MAX_COST_SETTING)
    exchange_rate_mxn_per_usd: float | None = Field(default=None, gt=0, le=_MAX_COST_SETTING)


class TenantProfitabilityOut(BaseModel):
    tenant_id: uuid.UUID
    tenant_name: str
    active_devices: int
    bytes_this_month: int
    # ESTIMATED cost, not exact -- see the platform_billing_settings comment.
    estimated_cost_usd: float
    estimated_cost_mxn: float
    # EQUIVALENT monthly revenue -- lines of a tenant on a semiannual/annual
    # cycle are normalized to a comparable monthly value (÷6/÷12) so margin
    # can be computed against cost, which is monthly by nature.
    monthly_revenue_mxn: float
    margin_mxn: float
    # None when the tenant has no contracted revenue yet (avoids division by
    # zero, not a margin of -infinity).
    margin_pct: float | None


# --- Real data usage per SIM line (device_data_usage_monthly) --
# PLATFORM-ONLY. ---
class DeviceDataUsageMonthOut(BaseModel):
    year_month: str  # "YYYY-MM-01", first day of the month
    bytes_rx: int
    bytes_tx: int


class DeviceDataUsageOut(BaseModel):
    device_id: uuid.UUID
    tenant_id: uuid.UUID
    tenant_name: str
    label: str
    device_model_name: str | None
    sim_number: str | None
    sim_carrier: str | None
    sim_plan_cost_mxn_month: float | None
    sim_plan_data_cap_mb: int | None
    months: list[DeviceDataUsageMonthOut]
    total_bytes_12m: int
    # Average over the months that DO have data, never a fixed 12 -- a
    # 2-month-old device must not look artificially low. None if no month
    # has data.
    avg_monthly_bytes: float | None
    # true if the MOST RECENT month with data exceeded sim_plan_data_cap_mb --
    # the signal for when to renegotiate/switch plans.
    over_cap: bool


# ---------------------------------------------------------------------------
# platform_monitoring_settings -- configurable "device alive/seen now"
# threshold (0028_platform_monitoring_settings.sql). Unlike
# platform_billing_settings, NOT sensitive -- readable by any authenticated
# session (a tenant/driver session needs the same value to render its own
# devices' status consistently with the platform). Only editing is
# bypass-only.
# ---------------------------------------------------------------------------
class PlatformMonitoringSettingsOut(BaseModel):
    device_offline_threshold_seconds: int
    updated_at: str


# Sanity cap, not a business limit -- prevents an absurd value (e.g. days)
# that would make EVERY device look "alive" forever. 24h is generous for any
# real field-device heartbeat interval (JT808 or GT06).
_MAX_OFFLINE_THRESHOLD_SECONDS = 24 * 60 * 60


class PlatformMonitoringSettingsUpdate(BaseModel):
    device_offline_threshold_seconds: int = Field(gt=0, le=_MAX_OFFLINE_THRESHOLD_SECONDS)


# ---------------------------------------------------------------------------
# platform_map_settings (0030_platform_map_settings.sql) -- map tile
# provider. "auto" (default) delegates to the FRONTEND's automatic failover
# on tile errors (web/src/lib/mapProviders.ts) -- this value never decides
# which provider is used in 'auto'; only the browser does, in real time. Any
# other value forces that provider platform-wide -- a manual escape hatch,
# never the expected path.
# ---------------------------------------------------------------------------
MapProvider = Literal["auto", "osm", "esri", "carto"]


class PlatformMapSettingsOut(BaseModel):
    active_provider: MapProvider
    forced_by_email: str | None
    forced_at: str | None
    updated_at: str


class PlatformMapSettingsUpdate(BaseModel):
    active_provider: MapProvider


# ---------------------------------------------------------------------------
# device_commands (0029_device_commands.sql) -- remote commands to devices
# (engine cut/restore, GT06 today). command_type is protocol-AGNOSTIC on
# purpose: this schema knows nothing about GT06/0x80/DYD#; that lives
# entirely in jt808-server/internal/gt06server.
# ---------------------------------------------------------------------------
DeviceCommandType = Literal["engine_stop", "engine_resume"]
DeviceCommandStatus = Literal["pending", "success", "failed", "timeout", "device_offline"]


class DeviceCommandCreate(BaseModel):
    command_type: DeviceCommandType


class DeviceCommandOut(BaseModel):
    id: uuid.UUID
    device_id: uuid.UUID
    command_type: DeviceCommandType
    requested_by: uuid.UUID
    requested_by_email: str
    requested_at: str
    status: DeviceCommandStatus
    device_reply: str | None
    completed_at: str | None


# ---------------------------------------------------------------------------
# GT06 CONFIGURATION commands (SERVER/APN/TIMEZONE/UPLOAD/FILELIST/UPLOADSW/
# TIMER/ANGLEREP/SOSALM/COREKITSW/...) -- distinct from DeviceCommand above
# (protocol-agnostic, usable by tenant_admin): this is 100% GT06-specific and
# PLATFORM-ONLY. See app/gt06_config_commands.py (command catalog +
# validation).
# ---------------------------------------------------------------------------
DeviceConfigCommandStatus = Literal["pending", "success", "failed", "timeout", "device_offline"]


class DeviceConfigCommandCreate(BaseModel):
    command_key: DeviceConfigCommandKey
    # Actually validated against the command-specific Pydantic model in
    # gt06_config_commands.build_raw_command -- a generic dict here to avoid
    # duplicating the whole field catalog in two places.
    params: dict[str, object] = {}


class DeviceConfigCommandOut(BaseModel):
    id: uuid.UUID
    device_id: uuid.UUID
    command_key: DeviceConfigCommandKey
    params: dict[str, object]
    raw_text: str
    requested_by: uuid.UUID
    requested_by_email: str
    requested_at: str
    status: DeviceConfigCommandStatus
    device_reply: str | None
    completed_at: str | None


# --- Geofences (0052_geofences.sql). Geometry is validated HERE (clean 422)
# and AGAIN in the database (geofences_prepare + CHECKs): the API is never
# the only barrier.
GeofenceShape = Literal["circle", "polygon"]
GeofenceEventType = Literal["enter", "exit", "dwell"]

_HEX_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
_MAX_POLYGON_VERTICES = 500
_MAX_GEOFENCE_DEVICE_IDS = 1000


def _validate_polygon(poly: list[tuple[float, float]]) -> list[tuple[float, float]]:
    if not (3 <= len(poly) <= _MAX_POLYGON_VERTICES):
        raise ValueError(f"a polygon needs between 3 and {_MAX_POLYGON_VERTICES} vertices")
    for lat, lon in poly:
        if not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
            raise ValueError("vertex out of latitude/longitude range")
    # Area (shoelace, in degrees²) -- rejects degenerate polygons (all points
    # collinear or repeated): they would never contain anything.
    area2 = 0.0
    for i, (lat_i, lon_i) in enumerate(poly):
        lat_j, lon_j = poly[(i + 1) % len(poly)]
        area2 += lon_i * lat_j - lon_j * lat_i
    if abs(area2) < 1e-10:
        raise ValueError("the polygon has no area (collinear or repeated vertices)")
    return poly


class _GeofenceFields(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=120)
    description: str | None = Field(None, max_length=1000)
    color: str | None = None
    shape: GeofenceShape | None = None
    center_lat: float | None = Field(None, ge=-90, le=90)
    center_lon: float | None = Field(None, ge=-180, le=180)
    radius_m: float | None = Field(None, ge=10, le=100_000)
    polygon: list[tuple[float, float]] | None = None
    enabled: bool | None = None
    notify_on_enter: bool | None = None
    notify_on_exit: bool | None = None
    dwell_minutes: int | None = Field(None, ge=1, le=10_080)
    severity: AlarmSeverity | None = None
    hysteresis_m: int | None = Field(None, ge=0, le=500)
    applies_to_all_devices: bool | None = None
    device_ids: list[uuid.UUID] | None = Field(None, max_length=_MAX_GEOFENCE_DEVICE_IDS)

    @field_validator("color")
    @classmethod
    def _check_color(cls, v: str | None) -> str | None:
        if v is not None and not _HEX_COLOR_RE.match(v):
            raise ValueError("color must be hexadecimal #RRGGBB")
        return v

    @field_validator("polygon")
    @classmethod
    def _check_polygon(cls, v: list[tuple[float, float]] | None) -> list[tuple[float, float]] | None:
        return None if v is None else _validate_polygon(v)

    def _check_geometry(self) -> None:
        geometry_given = any(
            getattr(self, f) is not None for f in ("center_lat", "center_lon", "radius_m", "polygon")
        )
        if self.shape is None:
            if geometry_given:
                raise ValueError("'shape' is required to change the geometry")
            return
        if self.shape == "circle":
            if None in (self.center_lat, self.center_lon, self.radius_m) or self.polygon is not None:
                raise ValueError("a circle requires center_lat, center_lon and radius_m (no polygon)")
        elif self.polygon is None or any(
            getattr(self, f) is not None for f in ("center_lat", "center_lon", "radius_m")
        ):
            raise ValueError("a polygon requires polygon (no center_lat/center_lon/radius_m)")


class GeofenceCreate(_GeofenceFields):
    # Optional for a tenant session (its own tenant is used); a platform
    # session MUST provide it -- see geofences.py.
    tenant_id: uuid.UUID | None = None
    name: str = Field(min_length=1, max_length=120)
    shape: GeofenceShape

    @model_validator(mode="after")
    def _geometry(self) -> "GeofenceCreate":
        self._check_geometry()
        return self


class GeofenceUpdate(_GeofenceFields):
    # dwell_minutes explicitly null = remove dwell; distinguished from "not
    # sent" via model_fields_set in the router.
    @model_validator(mode="after")
    def _geometry(self) -> "GeofenceUpdate":
        self._check_geometry()
        return self


class GeofenceOut(BaseModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    name: str
    description: str | None
    color: str
    shape: GeofenceShape
    center_lat: float | None
    center_lon: float | None
    radius_m: float | None
    polygon: list[tuple[float, float]] | None
    enabled: bool
    notify_on_enter: bool
    notify_on_exit: bool
    dwell_minutes: int | None
    severity: AlarmSeverity
    hysteresis_m: int
    applies_to_all_devices: bool
    # Only the devices THIS session can see (geofence_devices RLS).
    device_ids: list[uuid.UUID]
    # Units (visible to this session) currently inside the geofence.
    inside_count: int
    created_at: str
    updated_at: str


class GeofenceOccupant(BaseModel):
    device_id: uuid.UUID
    device_label: str
    entered_at: str | None
    entry_estimated: bool


class GeofenceEventOut(BaseModel):
    id: uuid.UUID
    geofence_id: uuid.UUID | None
    geofence_name: str
    device_id: uuid.UUID
    device_label: str
    event_type: GeofenceEventType
    time: str
    lat: float
    lon: float
    speed_kmh: float | None
    entered_at: str | None
    duration_s: int | None
    entry_estimated: bool
    alarm_id: uuid.UUID | None


class GeofenceReportRow(BaseModel):
    geofence_id: uuid.UUID | None
    geofence_name: str
    enters: int
    exits: int
    dwells: int
    unique_devices: int
    total_inside_s: int
    avg_visit_s: int | None


class GeofenceVisit(BaseModel):
    device_id: uuid.UUID
    device_label: str
    geofence_id: uuid.UUID | None
    geofence_name: str
    entered_at: str | None
    exited_at: str | None
    duration_s: int | None
    entry_estimated: bool
    # true = the unit is still inside (open visit, duration so far).
    open: bool


class GeofenceReport(BaseModel):
    date_from: str
    date_to: str
    geofences: list[GeofenceReportRow]
    visits: list[GeofenceVisit]
    visits_truncated: bool
