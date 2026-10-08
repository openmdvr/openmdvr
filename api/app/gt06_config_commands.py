"""GT06 configuration command catalog for Jimi IoT JC261/JC400 dashcams.

Sources (syntax is never guessed):

1. Public vendor configuration guide for Jimi IoT dashcams -- base syntax for
   SERVER/APN/TIMEZONE/UPLOAD/FILELIST/UPLOADSW/TIMER/ANGLEREP/SOSALM/
   COREKITSW, confirmed against a physical JC261.
2. A second, independent public knowledge base for the JC261/JC400 series --
   used to confirm MILEAGE/TIMESYNC/TIMER1/ANGLEREP/SOSALM (matching source 1)
   and to add ACCREP, CRASHALM/RAPIDACC/RAPIDDEC/RAPIDTURN sensitivity,
   RAPIDTEST, REBOOT, UART and RSERVICE (the command that sets where the
   device pushes its live RTMP video). Two independent sources agreeing on
   the overlapping commands raises confidence in the whole catalog.
3. A generic DVR command list (SENALM/RECORDAUDIO/VOLUME/EXDEVICESW/SENSOR/
   SHOCK/MILE/DEFENSE_TIME/SHUTDOWNTIME/EXBATALM/FATIGUE/FILTER/COLLIDE/
   Video/Picture/SPEED/UPDATE) -- lower confidence than 1-2 (single source,
   not yet confirmed against real hardware). Implemented exactly as the
   documented examples; adjust once confirmed against a physical device.

The raw text is ALWAYS built here, never sent by the client: an extra `#`
in a parameter (e.g. a malicious host) would otherwise terminate the GT06
command early and inject a second arbitrary command into the same string.
Each builder validates its own parameters with Pydantic before
interpolating anything.

Security: sending ANY command from this catalog requires
`require_super_admin` (see api/app/routers/device_config_commands.py) --
stricter than the usual platform-only `require_bypass` (which includes
`support`). These commands can cut a real device off from the platform,
flash arbitrary firmware, or disable safety sensors (crash/fatigue), so not
even `support` may send them. Viewing the history (GET) stays on
`require_bypass`, mirroring "create an API key" vs. "list/revoke it"."""
from __future__ import annotations

import re
from typing import Callable, Literal

from pydantic import BaseModel, Field, field_validator

DeviceConfigCommandKey = Literal[
    # Source 1 -- confirmed against real hardware.
    "corekitsw", "server", "apn", "upload_url", "filelist_url",
    "uploadsw", "timezone", "timer", "anglerep", "sosalm",
    # Source 2 -- confirmed/refined by a second independent source.
    "mileage", "timesync", "timer_acc_off", "accrep", "crashalm",
    "rapidacc_sensitivity", "rapiddec_sensitivity", "rapidturn_sensitivity",
    "rapidtest", "reboot", "uart", "rservice",
    # Source 3 -- single source, not yet confirmed against real hardware.
    "senalm", "recordaudio", "recordaudio_sub", "volume", "exdevicesw",
    "sensor", "shock", "mile", "defense_time", "shutdowntime", "exbatalm",
    "fatigue", "filter", "collide", "video_capture", "picture_capture",
    "speed", "update_firmware",
    # Vendor hibernation FAQ -- documented for JC450/JC181; unconfirmed on
    # the JC261/JC400.
    "wakeup_query",
]

# Commands that, if misconfigured, can leave the device unable to reach this
# platform (or without live video, or with broken firmware). The frontend
# requires the same friction as "Cut engine" (typing the device label).
HIGH_RISK_COMMAND_KEYS: frozenset[str] = frozenset({"server", "apn", "rservice", "update_firmware"})

# Commands the device does NOT reply to by design (vendor-documented). A
# timeout is not a failure for these: the command went out and the result
# shows up in the device's behavior.
NO_REPLY_COMMAND_KEYS: frozenset[str] = frozenset({"wakeup_query"})

_HOST_RE = re.compile(r"^[A-Za-z0-9.-]{1,253}$")
# Restricts UPDATE to the vendor's firmware distribution hosts (every known
# OTA URL lives under this domain) -- never an arbitrary host. The pattern
# requires the FIRST "/" to come right after ".aliyuncs.com", so a trick
# like "aliyuncs.com.evil.com" cannot slip through.
_FIRMWARE_URL_RE = re.compile(r"^https://[A-Za-z0-9.-]+\.aliyuncs\.com/[\w.\-/%]+$")


def _validate_host(v: str) -> str:
    if not _HOST_RE.match(v):
        raise ValueError("invalid host -- only letters, digits, '.' and '-' are allowed")
    return v


class CorekitswParams(BaseModel):
    """No parameters -- unlocks custom video server configuration, a
    documented prerequisite for SERVER/APN/UPLOAD/etc."""


class ServerParams(BaseModel):
    # Defaults are the values confirmed against a real JC261 (mode=1, port
    # 5023, the GT06 server port), not the generic guide example (mode=0,
    # port 47755), which did not work on real hardware.
    mode: int = Field(1, ge=0, le=1)
    host: str
    port: int = Field(5023, ge=1, le=65535)

    _validate = field_validator("host")(_validate_host)


class ApnParams(BaseModel):
    name: str = Field(..., min_length=1, max_length=40)
    apn: str = Field(..., min_length=1, max_length=40)
    user: str = Field("", max_length=40)
    password: str = Field("", max_length=40)


class UploadUrlParams(BaseModel):
    # Default port 8083 matches ALARM_CLIP_LISTEN_ADDR (see
    # infra/docker-compose.yml). `host` has no default on purpose: it must be
    # written explicitly so it always points at THIS platform's server.
    host: str
    port: int = Field(8083, ge=1, le=65535)

    _validate = field_validator("host")(_validate_host)


class FilelistUrlParams(BaseModel):
    # Same rationale as UploadUrlParams -- port 8083.
    host: str
    port: int = Field(8083, ge=1, le=65535)

    _validate = field_validator("host")(_validate_host)


class UploadswParams(BaseModel):
    # The 5 documented event types for this device -- adding a newly
    # confirmed one is one more entry here, never a redesign.
    alarm_type: Literal["SOS", "CRASH", "RAPIDACC", "RAPIDDEC", "RAPIDTURN"]
    enabled: bool


class TimezoneParams(BaseModel):
    # Signed offset, e.g. "-07:00" -- critical so recorded clip timestamps
    # (EVENT_..._HH_MM_SS_...) match real time (see protocol 0x95 handling).
    offset: str = Field(..., pattern=r"^[+-]\d{2}:\d{2}$")


class TimerParams(BaseModel):
    # Reporting interval while ACC (ignition) is on. Confirmed by two
    # independent sources ("TIMER,ON,60"). 5 s floor so this cannot be used
    # to flood the device/server with reports; generous ceiling.
    seconds: int = Field(..., ge=5, le=3600)


class AnglerepParams(BaseModel):
    degrees: int = Field(..., ge=1, le=180)


class SosalmParams(BaseModel):
    """No parameters -- enables the device's own SOS alarm function
    (SOSALM,ON,0), independent of the physical button already covered by
    the base protocol."""


# --- Confirmed/refined by a second independent source ---


class MileageParams(BaseModel):
    """Enables the odometer. `initial_meters` is optional -- without it the
    command is `MILEAGE,ON#` (starts at 0); with it the initial value is set
    in meters (e.g. `MILEAGE,ON,86154000` starts at 86,154 km). There is no
    documented command to disable it, so none is invented."""

    initial_meters: int | None = Field(None, ge=0)


class TimesyncParams(BaseModel):
    """No parameters -- syncs the device clock to GPS (`TIMESYNC,gps#`)
    instead of cellular network time. A badly synced clock is the most
    common cause of "video not found" when requesting recorded clips."""


class TimerAccOffParams(BaseModel):
    """Reporting interval while ACC (ignition) is OFF -- `TIMER1,ON,
    {seconds}#`, counterpart of `timer`. 24 h ceiling: a longer interval has
    no practical use and would make the device look offline for too long."""

    seconds: int = Field(..., ge=5, le=86400)


class AccrepParams(BaseModel):
    """Enables/disables reporting ACC (ignition) state changes as a
    dedicated event -- complements the ignition bit the platform already
    reads from heartbeats/alarms."""

    enabled: bool


class SensitivityParams(BaseModel):
    """Sensitivity level 1 (low) to 3 (high) -- documented range for
    CRASHALM/RAPIDACC/RAPIDDEC/RAPIDTURN."""

    sensitivity: int = Field(..., ge=1, le=3)


class RapidtestParams(BaseModel):
    """Thresholds for the combined "aggressive driving" alert. The source
    documents `RAPIDTEST,30,40,70` without detailing each position beyond
    "the threshold that triggers the alert". Implemented as documented;
    adjust once confirmed against real hardware."""

    accel_threshold: int = Field(..., ge=1, le=200)
    decel_threshold: int = Field(..., ge=1, le=200)
    turn_threshold: int = Field(..., ge=1, le=200)


class WakeupQueryParams(BaseModel):
    """No parameters -- `WAKEUP_QUERY` wakes the device from hibernation
    (after ignition off). Vendor documentation: "the platform sends
    WAKEUP_QUERY command (this command will not be replied), about 30
    seconds, the device will connect". Documented for JC450/JC181; not
    documented for JC261/JC400. The device does NOT reply: success shows as
    the device reporting again (heartbeat/position) within ~30 s."""


class RebootParams(BaseModel):
    """No parameters -- reboots the device so pending configuration takes
    effect (documented as the final step after a batch of commands). Causes
    a real temporary disconnection while the device restarts -- not "high
    risk" like server/apn/rservice (it does not change WHERE it connects),
    but it interrupts service for seconds/minutes."""


class UartParams(BaseModel):
    """Wired sensor (door/seatbelt) -- `UART,<A>,<B>,<C>,<D>,<E>,<F>#`.
    Fully documented layout: A=trigger mode, B=ACC state in which it
    detects, C=seconds between detections (avoids duplicate videos), D=max
    GPS speed for it to count (0=no limit), E=action on trigger, F=voice
    prompt."""

    trigger_mode: Literal["disabled", "trigger_on_close", "trigger_on_open"] = "trigger_on_close"
    acc_state: Literal["any", "acc_on", "acc_off"] = "any"
    interval_seconds: int = Field(60, ge=1, le=3600)
    max_speed_kmh: int = Field(100, ge=0, le=120)
    action: Literal["short_video", "photo"] = "short_video"
    voice_broadcast: Literal["none", "seatbelt", "door_sensor"] = "none"


_UART_TRIGGER_MODE = {"disabled": 0, "trigger_on_close": 1, "trigger_on_open": 2}
_UART_ACC_STATE = {"any": 0, "acc_on": 1, "acc_off": 2}
_UART_ACTION = {"short_video": 1, "photo": 2}
_UART_VOICE = {"none": 0, "seatbelt": 1, "door_sensor": 2}


class RserviceParams(BaseModel):
    """Sets where the device pushes its live RTMP video --
    `RSERVICE,rtmp://<host>:<port>/<app>#` (RTMP,ON/OFF only start/stop the
    push toward the destination configured here). High risk: a wrong value
    leaves the device unable to publish video, even though telemetry
    (position/alarms, via SERVER) keeps working.

    `host` has no default on purpose -- it must always be written explicitly
    to point at THIS platform, never at a third-party integrator's server.
    `port`/`app` default to values this platform already uses: 1935 is
    ZLMediaKit's RTMP port (`infra/zlmediakit/config.ini`, `[rtmp]`), and
    "live" is the default `GT06_VIDEO_APP` (`infra/docker-compose.yml`), the
    app name used to authorize the push (see `gt06videobridge.Bridge.App()`)."""

    host: str
    port: int = Field(1935, ge=1, le=65535)
    app: str = Field("live", pattern=r"^[A-Za-z0-9_-]{1,40}$")

    _validate = field_validator("host")(_validate_host)


# --- Single source, not yet confirmed against real hardware ---


class RecordaudioParams(BaseModel):
    """Enables/disables audio recording (main microphone)."""

    enabled: bool


class RecordaudioSubParams(BaseModel):
    """Enables/disables audio recording on the secondary (cabin)
    channel/microphone."""

    enabled: bool


class VolumeParams(BaseModel):
    """Device speaker volume, 0 (muted) to 15 -- typical range for this
    hardware class; the exact ceiling is not yet confirmed on a JC261."""

    level: int = Field(..., ge=0, le=15)


class ExdeviceswParams(BaseModel):
    """Enables/disables the external RFID reader -- the documented example
    uses `EXDEVICESW,2#` to enable and `EXDEVICESW,0#` to disable (not a
    plain 1/0)."""

    enabled: bool


class SensorParams(BaseModel):
    """Advanced CRASHALM sensitivity -- a raw value (0-255) with no
    breakdown documented beyond the example `SENSOR,255#`."""

    value: int = Field(..., ge=0, le=255)


class ShockParams(BaseModel):
    """Advanced SENALM sensitivity -- raw value with no breakdown documented
    beyond the example `SHOCK,20#`."""

    value: int = Field(..., ge=1, le=100)


class SenalmParams(BaseModel):
    """Enables the vibration alarm while ignition is on, with a sensitivity
    level (documented example: `SENALM,ON,2#`)."""

    sensitivity: int = Field(..., ge=1, le=3)


class MileParams(BaseModel):
    """Switches the reported speed unit from KPH to MPH (`MILE,1#`) or back
    to KPH (`MILE,0#`)."""

    use_mph: bool


class DefenseTimeParams(BaseModel):
    """Minutes of delay before defense (anti-theft) mode activates after
    ignition off."""

    minutes: int = Field(..., ge=0, le=60)


class ShutdowntimeParams(BaseModel):
    """Minutes of delay before the device powers down after ignition off
    (documented example: `SHUTDOWNTIME,30#`)."""

    minutes: int = Field(..., ge=1, le=120)


class ExbatalmParams(BaseModel):
    """Low external (vehicle) battery alarm -- two raw parameters
    (`EXBATALM,0,115#` in the documented example); the exact meaning of the
    first one (mode/enabled?) is unconfirmed, implemented as documented."""

    mode: int = Field(..., ge=0, le=1)
    voltage: int = Field(..., ge=1, le=999)


class FatigueParams(BaseModel):
    """Fatigue driving alert -- two raw parameters (`FATIGUE,ON,4,5#` in the
    documented example, presumably continuous driving hours + sensitivity);
    exact order unconfirmed."""

    param_a: int = Field(..., ge=1, le=180)
    param_b: int = Field(..., ge=1, le=10)


class FilterParams(BaseModel):
    """Crash event filter window (`FILTER,CRASH,{value}#`, documented
    example `FILTER,CRASH,5#`)."""

    seconds: int = Field(..., ge=1, le=60)


class CollideParams(BaseModel):
    """Impact tolerance -- 7 raw numeric parameters
    (`COLLIDE,ON,0,225,0,15,6,70,200#` in the documented example). None of
    the 7 has a documented meaning beyond the full example; implemented
    positionally, to be adjusted once confirmed against real hardware."""

    p1: int = Field(..., ge=0, le=999)
    p2: int = Field(..., ge=0, le=999)
    p3: int = Field(..., ge=0, le=999)
    p4: int = Field(..., ge=0, le=999)
    p5: int = Field(..., ge=0, le=999)
    p6: int = Field(..., ge=0, le=999)
    p7: int = Field(..., ge=0, le=999)


class VideoCaptureParams(BaseModel):
    """Triggers a one-off recording from the interior or exterior camera --
    `Video,{in|out},{seconds}s#` (documented example `Video,in,3s`). An
    immediate ACTION, not persistent configuration."""

    direction: Literal["in", "out"]
    duration_seconds: int = Field(3, ge=1, le=60)


class PictureCaptureParams(BaseModel):
    """Triggers a one-off photo from one or both cameras -- `Picture,{in|out|
    inout}#`. Immediate action, no duration parameter."""

    mode: Literal["in", "out", "inout"]


class SpeedParams(BaseModel):
    """Overspeed alert reported by the device itself -- 3 raw parameters
    (`SPEED,ON,10,90,1#` in the documented example, presumably margin/km/h
    threshold + something else). The platform already has its own
    protocol-agnostic max-speed guardrail; this is the device's NATIVE
    alarm, redundant but usable as an extra hardware-side signal."""

    p1: int = Field(..., ge=0, le=999)
    p2: int = Field(..., ge=0, le=999)
    p3: int = Field(..., ge=0, le=999)


class UpdateFirmwareParams(BaseModel):
    """Updates device firmware from a vendor URL -- HIGH risk (can brick the
    device if interrupted or if the version is incompatible; the vendor
    warns updates must be applied IN ORDER, never skipping versions). The
    URL is restricted to the vendor's firmware distribution domain
    (`*.aliyuncs.com`) -- never an arbitrary host."""

    url: str = Field(..., max_length=260)

    @field_validator("url")
    @classmethod
    def _validate_firmware_url(cls, v: str) -> str:
        if not _FIRMWARE_URL_RE.match(v):
            raise ValueError("invalid firmware URL -- must be https://*.aliyuncs.com/...")
        return v


_REGISTRY: dict[DeviceConfigCommandKey, tuple[type[BaseModel], Callable]] = {
    "corekitsw": (CorekitswParams, lambda p, imei: "COREKITSW,0#"),
    "server": (ServerParams, lambda p, imei: f"SERVER,{p.mode},{p.host},{p.port}#"),
    "apn": (ApnParams, lambda p, imei: f"APN,{p.name},{p.apn},,,,,,{p.user},,{p.password},,,,#"),
    "upload_url": (UploadUrlParams, lambda p, imei: f"UPLOAD,http://{p.host}:{p.port}/upload/{imei}#"),
    "filelist_url": (FilelistUrlParams, lambda p, imei: f"FILELIST,http://{p.host}:{p.port}/filelist/{imei}#"),
    "uploadsw": (UploadswParams, lambda p, imei: f"UPLOADSW,{p.alarm_type},{'ON' if p.enabled else 'OFF'}#"),
    "timezone": (TimezoneParams, lambda p, imei: f"TIMEZONE,{p.offset}#"),
    "timer": (TimerParams, lambda p, imei: f"TIMER,ON,{p.seconds}#"),
    "anglerep": (AnglerepParams, lambda p, imei: f"ANGLEREP,ON,{p.degrees}#"),
    "sosalm": (SosalmParams, lambda p, imei: "SOSALM,ON,0#"),
    "mileage": (
        MileageParams,
        lambda p, imei: f"MILEAGE,ON,{p.initial_meters}#" if p.initial_meters is not None else "MILEAGE,ON#",
    ),
    "timesync": (TimesyncParams, lambda p, imei: "TIMESYNC,gps#"),
    "timer_acc_off": (TimerAccOffParams, lambda p, imei: f"TIMER1,ON,{p.seconds}#"),
    "accrep": (AccrepParams, lambda p, imei: f"ACCREP,{'ON' if p.enabled else 'OFF'}#"),
    "crashalm": (SensitivityParams, lambda p, imei: f"CRASHALM,ON,{p.sensitivity}#"),
    "rapidacc_sensitivity": (SensitivityParams, lambda p, imei: f"RAPIDACC,{p.sensitivity}#"),
    "rapiddec_sensitivity": (SensitivityParams, lambda p, imei: f"RAPIDDEC,{p.sensitivity}#"),
    "rapidturn_sensitivity": (SensitivityParams, lambda p, imei: f"RAPIDTURN,{p.sensitivity}#"),
    "rapidtest": (
        RapidtestParams,
        lambda p, imei: f"RAPIDTEST,{p.accel_threshold},{p.decel_threshold},{p.turn_threshold}#",
    ),
    "reboot": (RebootParams, lambda p, imei: "REBOOT#"),
    "wakeup_query": (WakeupQueryParams, lambda p, imei: "WAKEUP_QUERY#"),
    "uart": (
        UartParams,
        lambda p, imei: (
            f"UART,{_UART_TRIGGER_MODE[p.trigger_mode]},{_UART_ACC_STATE[p.acc_state]},"
            f"{p.interval_seconds},{p.max_speed_kmh},{_UART_ACTION[p.action]},{_UART_VOICE[p.voice_broadcast]}#"
        ),
    ),
    "rservice": (RserviceParams, lambda p, imei: f"RSERVICE,rtmp://{p.host}:{p.port}/{p.app}#"),
    "senalm": (SenalmParams, lambda p, imei: f"SENALM,ON,{p.sensitivity}#"),
    "recordaudio": (RecordaudioParams, lambda p, imei: f"RECORDAUDIO,{1 if p.enabled else 0}#"),
    "recordaudio_sub": (RecordaudioSubParams, lambda p, imei: f"RECORDAUDIO_SUB,{1 if p.enabled else 0}#"),
    "volume": (VolumeParams, lambda p, imei: f"VOLUME,{p.level}#"),
    "exdevicesw": (ExdeviceswParams, lambda p, imei: f"EXDEVICESW,{2 if p.enabled else 0}#"),
    "sensor": (SensorParams, lambda p, imei: f"SENSOR,{p.value}#"),
    "shock": (ShockParams, lambda p, imei: f"SHOCK,{p.value}#"),
    "mile": (MileParams, lambda p, imei: f"MILE,{1 if p.use_mph else 0}#"),
    "defense_time": (DefenseTimeParams, lambda p, imei: f"DEFENSE_TIME,{p.minutes}#"),
    "shutdowntime": (ShutdowntimeParams, lambda p, imei: f"SHUTDOWNTIME,{p.minutes}#"),
    "exbatalm": (ExbatalmParams, lambda p, imei: f"EXBATALM,{p.mode},{p.voltage}#"),
    "fatigue": (FatigueParams, lambda p, imei: f"FATIGUE,ON,{p.param_a},{p.param_b}#"),
    "filter": (FilterParams, lambda p, imei: f"FILTER,CRASH,{p.seconds}#"),
    "collide": (
        CollideParams,
        lambda p, imei: f"COLLIDE,ON,{p.p1},{p.p2},{p.p3},{p.p4},{p.p5},{p.p6},{p.p7}#",
    ),
    "video_capture": (VideoCaptureParams, lambda p, imei: f"Video,{p.direction},{p.duration_seconds}s#"),
    "picture_capture": (PictureCaptureParams, lambda p, imei: f"Picture,{p.mode}#"),
    "speed": (SpeedParams, lambda p, imei: f"SPEED,ON,{p.p1},{p.p2},{p.p3}#"),
    "update_firmware": (UpdateFirmwareParams, lambda p, imei: f"UPDATE,{p.url}#"),
}

# raw_text has CHECK char_length <= 300 in the migration (0041). None of the
# builders above can approach that with the length caps on each Params
# (host<=253, apn/user/password<=40, url<=260), but it is re-checked here in
# case a future builder ignores that discipline.
MAX_RAW_TEXT_LEN = 300


def build_raw_command(command_key: DeviceConfigCommandKey, params: dict, imei: str) -> str:
    """Validates `params` against the command's Pydantic model and builds
    the real GT06 raw text. Raises pydantic.ValidationError if params do not
    fit (the router maps it to 422), or ValueError if the result exceeds
    MAX_RAW_TEXT_LEN (defensive; should not happen with the caps above)."""
    model_cls, builder = _REGISTRY[command_key]
    parsed = model_cls(**params)
    text = builder(parsed, imei)
    if len(text) > MAX_RAW_TEXT_LEN:
        raise ValueError(f"resulting command ({len(text)} chars) exceeds the maximum of {MAX_RAW_TEXT_LEN}")
    return text
