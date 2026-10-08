// Thin HTTP client for the API. No axios or data-fetching framework -- native
// fetch() is enough for this dashboard.

const API_BASE = import.meta.env.VITE_API_BASE_URL ?? "http://127.0.0.1:8000";

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

function authHeaders(): HeadersInit {
  const token = localStorage.getItem("token");
  return token ? { Authorization: `Bearer ${token}` } : {};
}

// The JWT expires (JWT_EXPIRE_MINUTES) and the API then answers 401 to every
// request. RequireAuth (App.tsx) only checks that a token string exists in LOCAL
// state, never that it is still valid, so without this hook the dashboard would
// stay "logged in" showing empty pages. auth.tsx registers this handler when
// AuthProvider mounts to clear the session and let RequireAuth react to the
// token becoming null.
let onUnauthorized: (() => void) | null = null;
export function setUnauthorizedHandler(fn: () => void) {
  onUnauthorized = fn;
}

// Shared by request<T> (JSON) and requestBlob (binary, see requestSnapshot) --
// same error logic whether the expected body is JSON or an image, never
// duplicated.
async function throwForErrorResponse(resp: Response, path: string): Promise<never> {
  let message = resp.statusText;
  try {
    const body = await resp.json();
    message = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
  } catch {
    // body was not JSON, keep statusText
  }
  // 401 on /auth/login means "invalid credentials" (handled by Login.tsx) -- not
  // a session to close, so it is excluded on purpose.
  if (resp.status === 401 && path !== "/auth/login") onUnauthorized?.();
  throw new ApiError(resp.status, message);
}

// Native fetch() NEVER times out on its own: on a real cellular network a
// connection that "hangs" without ever completing (no data, no error) leaves the
// promise pending forever. CameraTile's PLAYBACK_STARTUP_TIMEOUT_MS only
// protects the NEXT step (playback starting after api.requestVideo resolved) --
// if requestVideo itself hangs, nothing else would notice. Single choke point:
// EVERY API call goes through request<T>/requestBlob, so a timeout here covers
// them all. Generous on purpose: the backend's gt06-video path can legitimately
// take up to ~30s (see video.py); a shorter timeout would produce false "network
// error"s in the slow-but-working case.
const REQUEST_TIMEOUT_MS = 40_000;

async function fetchWithTimeout(url: string, init?: RequestInit): Promise<Response> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
  try {
    return await fetch(url, { ...init, signal: controller.signal });
  } catch (err) {
    if (controller.signal.aborted) {
      throw new Error("la conexión tardó demasiado — verifica tu señal e intenta de nuevo");
    }
    throw err;
  } finally {
    clearTimeout(timer);
  }
}

// negotiateWebrtc: WHEP signaling for live video. Unlike request<T>/requestBlob,
// this talks DIRECTLY to ZLMediaKit (the `webrtcUrl` returned by
// api.requestVideo is already an absolute ticketed URL; it never goes through
// API_BASE or our JWT). Per ZLMediaKit's source (server/WebApi.cpp,
// whip_whep_func): success = 201 + Content-Type application/sdp + the answer SDP
// as a RAW body (never JSON) + a Location header with the session URL to
// terminate it with DELETE; error = 406 + text/plain + the real message (e.g.
// "no autorizado", "stream not found").
export interface WebrtcNegotiateResult {
  answerSdp: string;
  // WHEP session URL (for DELETE on teardown, a cleaner close than just closing
  // the RTCPeerConnection locally) -- null if ZLMediaKit did not send the header
  // (should not happen on a real 201, but never assumed).
  deleteUrl: string | null;
}

export async function negotiateWebrtc(webrtcUrl: string, offerSdp: string): Promise<WebrtcNegotiateResult> {
  const resp = await fetchWithTimeout(webrtcUrl, {
    method: "POST",
    headers: { "Content-Type": "application/sdp" },
    body: offerSdp,
  });
  const text = await resp.text();
  if (!resp.ok) {
    // An INFRASTRUCTURE failure (e.g. an intermediate proxy returning its own
    // error page) never comes as text/plain with a short message (that format is
    // EXCLUSIVE to ZLMediaKit errors, see above) -- otherwise a full HTML page
    // could be shown as the error message. The raw detail is always logged for
    // diagnosis, but the user never sees a proxy's HTML.
    // eslint-disable-next-line no-console
    console.error("CameraTile: WHEP negotiation failed", { status: resp.status, contentType: resp.headers.get("content-type"), body: text });
    const isPlainZlmError = (resp.headers.get("content-type") ?? "").startsWith("text/plain") && text.length < 200;
    throw new ApiError(resp.status, isPlainZlmError ? text || resp.statusText : "no se pudo negociar el video (falla de red o del servidor)");
  }
  return { answerSdp: text, deleteUrl: resp.headers.get("Location") };
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const resp = await fetchWithTimeout(`${API_BASE}${path}`, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...authHeaders(),
      ...(init?.headers ?? {}),
    },
  });
  if (!resp.ok) await throwForErrorResponse(resp, path);
  if (resp.status === 204) return undefined as T;
  return (await resp.json()) as T;
}

// requestBlob: same auth/error/timeout handling as request<T>, but for a BINARY
// response body (an image, never JSON) -- currently only used by
// requestSnapshot. Never goes through JSON.parse on the success path.
async function requestBlob(path: string, init?: RequestInit): Promise<Blob> {
  const resp = await fetchWithTimeout(`${API_BASE}${path}`, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...authHeaders(),
      ...(init?.headers ?? {}),
    },
  });
  if (!resp.ok) await throwForErrorResponse(resp, path);
  return await resp.blob();
}

export interface LoginResponse {
  access_token: string;
  role: string;
  user_id: string;
  tenant_id: string | null;
  tenant_display_name: string | null;
  tenant_logo_url: string | null;
}

export interface Tenant {
  id: string;
  name: string;
  status: string;
  max_live_view_seconds: number;
  live_view_monthly_quota_seconds: number;
  live_view_seconds_remaining: number;
  // Tenant self-service settings -- null when the tenant has not configured
  // anything.
  display_name: string | null;
  logo_url: string | null;
  meal_break_window_start: string | null;
  meal_break_window_end: string | null;
  max_shift_hours: number | null;
  // GPS position retention -- a plan/billing attribute (always has a value,
  // default 90 days, never null).
  gps_retention_days: number;
  // Billing cycle -- ALL of the tenant's active lines are invoiced together on
  // this period.
  billing_period: BillingPeriod;
  // Provisioning guardrails -- SUM of quantity over the active subscription
  // lines of each category, computed (never persisted). Zero active lines = zero
  // quota, not unlimited. Camera (jt808/gt06_video) and GPS (gt06) are
  // independent pools.
  camera_device_quota: number;
  gps_device_quota: number;
  // Outbound webhooks (0035_webhooks.sql) -- approved per tenant at the
  // platform's discretion: ONLY super_admin can change this (PATCH
  // /tenants/{id}), never tenant_admin self-service.
  webhooks_enabled: boolean;
}

// Outbound webhooks -- the "push" counterpart of API keys. See
// api/app/webhooks.py for the delivery design (HMAC, retries, circuit breaker).
export interface WebhookEndpoint {
  id: string;
  tenant_id: string;
  url: string;
  event_types: string[];
  enabled: boolean;
  consecutive_failures: number;
  disabled_at: string | null;
  disabled_reason: string | null;
  last_attempt_at: string | null;
  last_success_at: string | null;
  created_by: string | null;
  created_at: string;
}

export interface WebhookEndpointCreated extends WebhookEndpoint {
  // The only time the signing secret travels through the API -- copy it now;
  // losing it means rotating (never "recovering").
  secret: string;
}

export interface WebhookDelivery {
  id: string;
  event_type: string;
  status: "pending" | "success" | "failed" | "exhausted";
  attempt_count: number;
  next_attempt_at: string;
  response_status_code: number | null;
  last_error: string | null;
  created_at: string;
  delivered_at: string | null;
}

// gt06_video (JIMI JC261/JC400): GT06 telemetry + RTMP push video -- unlike gt06
// (GPS-only), it HAS a camera. Shares gt06_imei with gt06 (same connection
// identifier), not jt808_terminal_id.
export type DeviceProtocol = "jt808" | "gt06" | "gt06_video";

// The two protocols identified by IMEI (gt06_imei) instead of jt808_terminal_id
// -- see devices_protocol_identifier_match (migrations 0026/0039). Shared helper
// so this comparison is not repeated in every component that shows/edits a
// device identifier.
export function usesGT06Imei(protocol: DeviceProtocol): boolean {
  return protocol === "gt06" || protocol === "gt06_video";
}

// A device with a camera (can request live video) -- jt808 always, gt06 only in
// its gt06_video variant (JIMI JC261/JC400). Plain gt06 (GPS-only) never has
// one.
export function hasCamera(protocol: DeviceProtocol): boolean {
  return protocol !== "gt06";
}

export interface Device {
  id: string;
  tenant_id: string;
  protocol: DeviceProtocol;
  // Exactly one of the two is populated, depending on protocol -- see
  // devices_protocol_identifier_match (migration 0026).
  jt808_terminal_id: string | null;
  gt06_imei: string | null;
  label: string;
  // Installed vehicle, if any -- plate/make/model/driver live in Vehicle/Driver
  // (see those types), not here. Look them up in the already-loaded
  // vehicle/driver roster by vehicle_id/current_driver_id, same as tenant_id ->
  // Tenant in Dashboard.tsx.
  vehicle_id: string | null;
  notes: string | null;
  status: string;
  last_seen_at: string | null;
  // Audit of the last status change (0038_user_device_status_audit.sql) -- null
  // if it never changed from its creation default.
  status_changed_by: string | null;
  status_changed_at: string | null;
  // Model (device_models catalog) + SIM.
  // sim_plan_cost_mxn_month/sim_plan_data_cap_mb deliberately do NOT live here
  // (platform-only, see getDeviceDataUsage() below); sim_number/sim_carrier do,
  // visible to any session that can already see the device.
  device_model_id: string | null;
  device_model_name: string | null;
  sim_number: string | null;
  sim_carrier: string | null;
  // Telemetry reported by the device itself -- NEVER editable (not part of the
  // updateDevice body). null = this signal has not been reported yet.
  ignition_on: boolean | null;
  ignition_changed_at: string | null;
  power_connected: boolean | null;
  power_changed_at: string | null;
}

export interface DeviceModel {
  id: string;
  name: string;
  protocol: DeviceProtocol;
  notes: string | null;
  created_at: string;
}

export interface Vehicle {
  id: string;
  tenant_id: string;
  plate: string | null;
  make: string | null;
  model: string | null;
  year: number | null;
  status: string;
  notes: string | null;
  current_driver_id: string | null;
  current_driver_name: string | null;
  // NULL = no limit configured -- see 0050_vehicle_max_speed.sql.
  max_speed_kmh: number | null;
}

export interface Driver {
  id: string;
  tenant_id: string;
  name: string;
  license_number: string | null;
  phone: string | null;
  status: string;
  notes: string | null;
  current_vehicle_id: string | null;
  current_vehicle_plate: string | null;
}

// Pagination envelope -- same shape as Page[T] in schemas.py (api/).
export interface PageResult<T> {
  items: T[];
  total: number;
  limit: number;
  offset: number;
}

export interface ListParams {
  search?: string;
  limit?: number;
  offset?: number;
  // Optional -- narrows a listing to one tenant (per-tenant workspace). RLS
  // remains the real isolation; this never widens what a tenant session can
  // already see.
  tenant_id?: string;
  // /devices only: hides deactivated units (operational views).
  exclude_inactive?: boolean;
}

function listQuery(params?: ListParams): string {
  if (!params) return "";
  const q = new URLSearchParams();
  if (params.search) q.set("search", params.search);
  if (params.limit != null) q.set("limit", String(params.limit));
  if (params.offset != null) q.set("offset", String(params.offset));
  if (params.tenant_id) q.set("tenant_id", params.tenant_id);
  if (params.exclude_inactive) q.set("exclude_inactive", "true");
  const s = q.toString();
  return s ? `?${s}` : "";
}

// Tenant roles that can be created from the dashboard -- super_admin and support
// (PLATFORM_ROLES in the backend) are excluded on purpose; creating them
// requires super_admin specifically (see require_super_admin in api/app/deps.py)
// and is not a dashboard self-service flow.
export type TenantRole = "tenant_admin" | "tenant_operator" | "tenant_viewer" | "driver";

export interface User {
  id: string;
  email: string;
  role: string;
  tenant_id: string | null;
  driver_id: string | null;
  status: string;
  status_changed_by: string | null;
  status_changed_at: string | null;
}

// Device groups + user<->device assignment (alerts) -- see
// infra/postgres/migrations/0031_device_groups_and_assignments.sql. tenant_admin
// always sees its whole tenant without assignments -- these types only matter
// for tenant_operator/tenant_viewer.
export interface DeviceGroup {
  id: string;
  tenant_id: string;
  name: string;
  device_count: number;
}

export interface UserDeviceAssignments {
  device_ids: string[];
  device_group_ids: string[];
}

export interface UserNotificationSettings {
  in_app_enabled: boolean;
  email_enabled: boolean;
}

// API keys (0034_api_keys.sql) -- M2M integrations that authenticate AS this
// user, narrowed by can_write + allowed_device_ids. See
// api/app/deps.py::get_current_user for the actual enforcement.
export interface ApiKey {
  id: string;
  name: string;
  key_prefix: string;
  can_write: boolean;
  // null = unrestricted (everything the user can already see); [] = deliberately
  // no access to any device -- DIFFERENT from null, never conflate them.
  allowed_device_ids: string[] | null;
  created_at: string;
  expires_at: string;
  revoked_at: string | null;
  last_used_at: string | null;
  created_by: string | null;
  revoked_by: string | null;
}

export interface ApiKeyCreated extends ApiKey {
  // The only time the full key travels through the API -- show it to the user
  // once and never request/store it in any state that outlives the creation
  // flow.
  raw_key: string;
}

export interface ApiKeyUsage {
  occurred_at: string;
  method: string;
  path: string;
  status_code: number;
  ip_address: string | null;
}

export type ShiftEventType = "clock_in" | "clock_out" | "meal_start" | "meal_end";

export interface ShiftEvent {
  id: string;
  tenant_id: string;
  driver_id: string;
  driver_name: string;
  event_type: ShiftEventType;
  occurred_at: string;
  lat: number | null;
  lon: number | null;
  source: "driver_app" | "manual_admin";
}

export type RouteStatus = "planned" | "in_progress" | "completed" | "cancelled";

export interface Route {
  id: string;
  tenant_id: string;
  name: string;
  description: string | null;
  date: string;
  driver_id: string | null;
  driver_name: string | null;
  vehicle_id: string | null;
  vehicle_plate: string | null;
  status: RouteStatus;
}

export interface DistanceDay {
  date: string;
  distance_km: number;
  position_count: number;
}

export interface VehicleDistanceReport {
  vehicle_id: string;
  device_id: string | null;
  date_from: string;
  date_to: string;
  days: DistanceDay[];
  total_distance_km: number;
}

export interface EngineHoursDay {
  date: string;
  driving_hours: number;
  idle_hours: number;
  engine_off_hours: number;
}

export interface VehicleEngineHoursReport {
  vehicle_id: string;
  device_id: string | null;
  date_from: string;
  date_to: string;
  days: EngineHoursDay[];
  total_driving_hours: number;
  total_idle_hours: number;
  total_engine_off_hours: number;
}

export interface WorkedDay {
  date: string;
  hours_worked: number;
  completed_shifts: number;
}

export interface DriverHoursReport {
  driver_id: string;
  date_from: string;
  date_to: string;
  days: WorkedDay[];
  total_hours: number;
}

export interface DevicePosition {
  device_id: string;
  label: string;
  lat: number;
  lon: number;
  speed_kmh: number | null;
  heading: number | null;
  time: string;
}

export type AlarmSeverity = "info" | "warning" | "critical";

// Route history (GET /devices/{id}/route-history). `points` is ALWAYS bounded by
// the backend, never by the client.
export interface RouteHistoryPoint {
  time: string;
  lat: number;
  lon: number;
  speed_kmh: number | null;
  heading: number | null;
}

export interface RouteHistoryEvent {
  id: string;
  time: string;
  alarm_type: string;
  severity: AlarmSeverity;
  lat: number | null;
  lon: number | null;
  has_video_clip: boolean;
}

// --- Geofences (0052_geofences.sql, api/app/routers/geofences.py) ---
export type GeofenceShape = "circle" | "polygon";
export type GeofenceEventType = "enter" | "exit" | "dwell";
export type LatLonTuple = [number, number];

export interface Geofence {
  id: string;
  tenant_id: string;
  name: string;
  description: string | null;
  color: string;
  shape: GeofenceShape;
  center_lat: number | null;
  center_lon: number | null;
  radius_m: number | null;
  polygon: LatLonTuple[] | null;
  enabled: boolean;
  notify_on_enter: boolean;
  notify_on_exit: boolean;
  dwell_minutes: number | null;
  severity: AlarmSeverity;
  hysteresis_m: number;
  applies_to_all_devices: boolean;
  device_ids: string[];
  inside_count: number;
  created_at: string;
  updated_at: string;
}

// Geometry: the backend requires it COMPLETE according to `shape` (never a
// circle with a polygon, nor a radius PATCH without shape).
export type GeofenceGeometry =
  | { shape: "circle"; center_lat: number; center_lon: number; radius_m: number }
  | { shape: "polygon"; polygon: LatLonTuple[] };

export interface GeofenceSettings {
  name: string;
  description: string | null;
  color: string;
  enabled: boolean;
  notify_on_enter: boolean;
  notify_on_exit: boolean;
  dwell_minutes: number | null;
  severity: AlarmSeverity;
  hysteresis_m: number;
  applies_to_all_devices: boolean;
  device_ids: string[];
}

export type GeofenceCreateBody = GeofenceSettings & GeofenceGeometry & { tenant_id?: string };
export type GeofenceUpdateBody = Partial<GeofenceSettings> & Partial<GeofenceGeometry>;

export interface GeofenceOccupant {
  device_id: string;
  device_label: string;
  entered_at: string | null;
  entry_estimated: boolean;
}

export interface GeofenceEvent {
  id: string;
  geofence_id: string | null;
  geofence_name: string;
  device_id: string;
  device_label: string;
  event_type: GeofenceEventType;
  time: string;
  lat: number;
  lon: number;
  speed_kmh: number | null;
  entered_at: string | null;
  duration_s: number | null;
  entry_estimated: boolean;
  alarm_id: string | null;
}

export interface GeofenceReportRow {
  geofence_id: string | null;
  geofence_name: string;
  enters: number;
  exits: number;
  dwells: number;
  unique_devices: number;
  total_inside_s: number;
  avg_visit_s: number | null;
}

export interface GeofenceVisit {
  device_id: string;
  device_label: string;
  geofence_id: string | null;
  geofence_name: string;
  entered_at: string | null;
  exited_at: string | null;
  duration_s: number | null;
  entry_estimated: boolean;
  open: boolean;
}

export interface GeofenceReport {
  date_from: string;
  date_to: string;
  geofences: GeofenceReportRow[];
  visits: GeofenceVisit[];
  visits_truncated: boolean;
}

export interface GeofenceEventQuery {
  from: string;
  to: string;
  deviceId?: string;
  geofenceId?: string;
  eventType?: GeofenceEventType;
  limit?: number;
  offset?: number;
}

export interface WebhookTestResult {
  success: boolean;
  status_code: number | null;
  error: string | null;
  elapsed_ms: number;
}

export interface RouteHistoryReport {
  device_id: string;
  date_from: string;
  date_to: string;
  points: RouteHistoryPoint[];
  bucket_seconds: number;
  events: RouteHistoryEvent[];
  events_truncated: boolean;
}

export interface DeviceHealthEvent {
  id: string;
  device_id: string;
  device_label: string;
  protocol: DeviceProtocol;
  tenant_name: string;
  kind: string;
  severity: AlarmSeverity;
  title: string;
  detail: Record<string, unknown>;
  occurrences: number;
  bytes_wasted: number;
  first_seen: string;
  last_seen: string;
  resolved_at: string | null;
}

export interface Alarm {
  id: string;
  device_id: string;
  device_label: string;
  alarm_type: string;
  severity: AlarmSeverity;
  time: string;
  acknowledged_at: string | null;
}

// Retrieval of alarm-linked video clips (GT06/JC261) -- requested ON DEMAND,
// never automatic. The backend never returns "unsupported" today (it only exists
// in the DB CHECK for a future phase), but it is typed anyway.
export type AlarmVideoClipStatus = "requested" | "uploading" | "ready" | "failed" | "unsupported";

export interface AlarmVideoClip {
  id: string;
  alarm_id: string;
  status: AlarmVideoClipStatus;
  requested_at: string;
  completed_at: string | null;
  error_detail: string | null;
  // Only populated when status === "ready" -- a short-lived signed R2 URL (see
  // storage.py), never persisted client side.
  url: string | null;
  // Cabin camera, when the device recorded both (migration 0044) -- independent
  // of `status`; it may arrive before or after (best-effort, chained after the
  // Front one). null is not an error, just "not yet" or "this event only had one
  // camera".
  url_secondary: string | null;
}

// In-app mailbox -- one row per (alarm, recipient), filtered by
// app_device_recipients() + the user's own channel preference, see
// infra/postgres/migrations/0033_notifications.sql.
export interface Notification {
  id: string;
  event_type: string;
  device_id: string | null;
  alarm_id: string | null;
  title: string;
  body: string | null;
  severity: AlarmSeverity | null;
  created_at: string;
  read_at: string | null;
}

export interface NotificationList {
  items: Notification[];
  total: number;
  unread_count: number;
  limit: number;
  offset: number;
}

// device_commands (0029_device_commands.sql) -- remote commands to devices (GT06
// engine cut/resume). command_type is protocol-agnostic on purpose, see the same
// comment in schemas.py.
export type DeviceCommandType = "engine_stop" | "engine_resume";
export type DeviceCommandStatus = "pending" | "success" | "failed" | "timeout" | "device_offline";

export interface DeviceCommand {
  id: string;
  device_id: string;
  command_type: DeviceCommandType;
  requested_by: string;
  requested_by_email: string;
  requested_at: string;
  status: DeviceCommandStatus;
  device_reply: string | null;
  completed_at: string | null;
}

// device_config_commands (0041_device_config_commands.sql, catalog extended in
// 0051) -- GT06 CONFIGURATION commands, see api/app/gt06_config_commands.py for
// the full catalog. SENDING them is super_admin ONLY (require_super_admin, not
// even support). Different from DeviceCommand above (engine, usable by
// tenant_admin).
export type DeviceConfigCommandKey =
  | "corekitsw"
  | "server"
  | "apn"
  | "upload_url"
  | "filelist_url"
  | "uploadsw"
  | "timezone"
  | "timer"
  | "anglerep"
  | "sosalm"
  | "mileage"
  | "timesync"
  | "timer_acc_off"
  | "accrep"
  | "crashalm"
  | "rapidacc_sensitivity"
  | "rapiddec_sensitivity"
  | "rapidturn_sensitivity"
  | "rapidtest"
  | "reboot"
  | "uart"
  | "rservice"
  | "senalm"
  | "recordaudio"
  | "recordaudio_sub"
  | "volume"
  | "exdevicesw"
  | "sensor"
  | "shock"
  | "mile"
  | "defense_time"
  | "shutdowntime"
  | "wakeup_query"
  | "exbatalm"
  | "fatigue"
  | "filter"
  | "collide"
  | "video_capture"
  | "picture_capture"
  | "speed"
  | "update_firmware";
export type DeviceConfigCommandStatus = "pending" | "success" | "failed" | "timeout" | "device_offline";

export interface DeviceConfigCommand {
  id: string;
  device_id: string;
  command_key: DeviceConfigCommandKey;
  params: Record<string, unknown>;
  raw_text: string;
  requested_by: string;
  requested_by_email: string;
  requested_at: string;
  status: DeviceConfigCommandStatus;
  device_reply: string | null;
  completed_at: string | null;
}

export interface DriverShiftStatus {
  driver_id: string;
  driver_name: string;
  last_event_type: ShiftEventType | null;
  last_event_at: string | null;
  last_lat: number | null;
  last_lon: number | null;
}

export type DriverShiftAlertType = "meal_outside_window" | "shift_exceeds_max_hours";

export interface DriverShiftAlert {
  id: string;
  driver_id: string;
  driver_name: string;
  alert_type: DriverShiftAlertType;
  details: Record<string, unknown> | null;
  occurred_at: string;
  acknowledged_at: string | null;
}

// Billing -- billing_plans is a global catalog, never readable by a tenant_admin
// (see 0020_billing_catalog.sql). These types are only used in Administration →
// Platform.
export type BillingPlanCategory = "gps" | "camera" | "addon";
export type BillingPeriod = "monthly" | "semiannual" | "annual";

export interface BillingPlan {
  id: string;
  name: string;
  sku: string;
  category: BillingPlanCategory;
  unit_price: number;
  currency: string;
  billing_period: BillingPeriod;
  active: boolean;
}

export interface TenantSubscriptionItem {
  id: string;
  tenant_id: string;
  billing_plan_id: string | null;
  plan_name: string | null;
  plan_sku: string | null;
  custom_description: string | null;
  quantity: number;
  unit_price_override: number | null;
  effective_unit_price: number;
  started_at: string;
  ended_at: string | null;
}

// Billing -- tenant_promotions is bypass-only in both directions (Administration
// → Platform); invoices ARE readable by the tenant_admin itself (basis of "My
// billing").
export type PromotionDiscountType = "full_waiver" | "percentage" | "fixed_amount";
export type InvoiceStatus = "draft" | "issued" | "paid" | "overdue" | "void";

export interface TenantPromotion {
  id: string;
  tenant_id: string;
  description: string;
  starts_at: string;
  ends_at: string;
  discount_type: PromotionDiscountType;
  discount_value: number;
}

export interface InvoiceLineItem {
  id: string;
  description: string;
  quantity: number;
  unit_price: number;
  subtotal: number;
}

export interface Invoice {
  id: string;
  tenant_id: string;
  period_start: string;
  period_end: string;
  issued_at: string;
  due_date: string;
  subtotal: number;
  discount_total: number;
  total: number;
  currency: string;
  status: InvoiceStatus;
  line_items: InvoiceLineItem[];
}

// Billing -- payments, always through PaymentProvider in the backend
// (api/app/payments.py). Recording is bypass-only; reading is
// require_tenant_admin (like invoices, a tenant sees its own payments).
export type PaymentMethod = "cash" | "bank_transfer" | "stripe" | "mercado_pago" | "other";

export interface Payment {
  id: string;
  invoice_id: string;
  tenant_id: string;
  amount: number;
  method: PaymentMethod;
  received_at: string;
  recorded_by: string | null;
  reference_note: string | null;
  external_provider: string | null;
  external_payment_id: string | null;
  invoice_status: InvoiceStatus;
}

// Billing -- estimated cost and margin. Never reachable by a tenant_admin,
// neither the cost assumptions nor the profitability report (see
// 0023_billing_cost_estimation.sql).
export interface PlatformBillingSettings {
  cost_usd_per_device_month: number;
  // Separate rate for a gt06 device (GPS-only, no video) -- deliberately lower
  // than cost_usd_per_device_month.
  cost_usd_per_gps_device_month: number;
  cost_usd_per_gb: number;
  exchange_rate_mxn_per_usd: number;
  updated_at: string;
}

export interface PlatformMonitoringSettings {
  device_offline_threshold_seconds: number;
  updated_at: string;
}

export type MapProvider = "auto" | "osm" | "esri" | "carto";

export interface PlatformMapSettings {
  active_provider: MapProvider;
  forced_by_email: string | null;
  forced_at: string | null;
  updated_at: string;
}

export interface TenantProfitability {
  tenant_id: string;
  tenant_name: string;
  active_devices: number;
  bytes_this_month: number;
  estimated_cost_usd: number;
  estimated_cost_mxn: number;
  monthly_revenue_mxn: number;
  margin_mxn: number;
  margin_pct: number | null;
}

// Real data usage per SIM line -- platform only, GET /billing/sim-usage.
export interface DeviceDataUsageMonth {
  year_month: string;
  bytes_rx: number;
  bytes_tx: number;
}

export interface DeviceDataUsage {
  device_id: string;
  tenant_id: string;
  tenant_name: string;
  label: string;
  device_model_name: string | null;
  sim_number: string | null;
  sim_carrier: string | null;
  sim_plan_cost_mxn_month: number | null;
  sim_plan_data_cap_mb: number | null;
  months: DeviceDataUsageMonth[];
  total_bytes_12m: number;
  avg_monthly_bytes: number | null;
  over_cap: boolean;
}

export const api = {
  login: (email: string, password: string) =>
    request<LoginResponse>("/auth/login", { method: "POST", body: JSON.stringify({ email, password }) }),

  listTenants: (params?: ListParams) => request<PageResult<Tenant>>(`/tenants${listQuery(params)}`),
  createTenant: (name: string) => request<Tenant>("/tenants", { method: "POST", body: JSON.stringify({ name }) }),
  updateTenant: (
    id: string,
    body: Partial<{
      max_live_view_seconds: number;
      live_view_monthly_quota_seconds: number;
      gps_retention_days: number;
      billing_period: BillingPeriod;
      webhooks_enabled: boolean;
    }>,
  ) =>
    request<Tenant>(`/tenants/${id}`, { method: "PATCH", body: JSON.stringify(body) }),
  // Not a per-column partial PATCH (see TenantSettingsUpdate in schemas.py) --
  // the body replaces all 5 fields every time; omitting the meal window/max
  // hours clears them to NULL.
  updateTenantSettings: (
    id: string,
    body: {
      display_name?: string | null;
      logo_url?: string | null;
      meal_break_window_start?: string | null;
      meal_break_window_end?: string | null;
      max_shift_hours?: number | null;
    },
  ) => request<Tenant>(`/tenants/${id}/settings`, { method: "PATCH", body: JSON.stringify(body) }),

  listDevices: (params?: ListParams) => request<PageResult<Device>>(`/devices${listQuery(params)}`),
  // Bypass-only on the backend, same rule as create_device -- only the platform
  // registers models (see DeviceModelCreate.protocol, require_super_admin).
  listDeviceModels: () => request<DeviceModel[]>("/devices/models"),
  createDeviceModel: (body: { name: string; protocol: DeviceProtocol; notes?: string }) =>
    request<DeviceModel>("/devices/models", { method: "POST", body: JSON.stringify(body) }),
  createDevice: (body: {
    tenant_id: string;
    protocol?: DeviceProtocol;
    jt808_terminal_id?: string;
    gt06_imei?: string;
    label: string;
    vehicle_id?: string;
    notes?: string;
    device_model_id?: string;
    sim_number?: string;
    sim_carrier?: string;
  }) => request<Device>("/devices", { method: "POST", body: JSON.stringify(body) }),
  updateDevice: (
    id: string,
    body: Partial<{
      label: string;
      vehicle_id: string;
      notes: string;
      status: "active" | "inactive" | "maintenance";
      // Only gt06<->gt06_video (same IMEI, only the classification changes) --
      // never jt808, that still requires removing and re-adding the device. See
      // DeviceUpdate.protocol in api/app/schemas.py.
      protocol: "gt06" | "gt06_video";
      device_model_id: string;
      sim_number: string;
      sim_carrier: string;
      // Never returned in the response (see Device above) -- write-only, read
      // later through getDeviceDataUsage().
      sim_plan_cost_mxn_month: number;
      sim_plan_data_cap_mb: number;
    }>,
  ) => request<Device>(`/devices/${id}`, { method: "PATCH", body: JSON.stringify(body) }),

  listVehicles: (params?: ListParams) => request<PageResult<Vehicle>>(`/vehicles${listQuery(params)}`),
  createVehicle: (body: {
    tenant_id: string;
    plate?: string;
    make?: string;
    model?: string;
    year?: number;
    notes?: string;
    max_speed_kmh?: number;
  }) => request<Vehicle>("/vehicles", { method: "POST", body: JSON.stringify(body) }),
  updateVehicle: (
    id: string,
    body: Partial<{ plate: string; make: string; model: string; year: number; notes: string; max_speed_kmh: number }>,
  ) =>
    request<Vehicle>(`/vehicles/${id}`, { method: "PATCH", body: JSON.stringify(body) }),
  assignDriver: (vehicleId: string, driverId: string) =>
    request<Vehicle>(`/vehicles/${vehicleId}/assign-driver`, { method: "POST", body: JSON.stringify({ driver_id: driverId }) }),
  unassignDriver: (vehicleId: string) =>
    request<Vehicle>(`/vehicles/${vehicleId}/unassign-driver`, { method: "POST" }),
  vehicleDistanceReport: (vehicleId: string, from: string, to: string) =>
    request<VehicleDistanceReport>(`/vehicles/${vehicleId}/distance?from=${from}&to=${to}`),
  vehicleEngineHoursReport: (vehicleId: string, from: string, to: string) =>
    request<VehicleEngineHoursReport>(`/vehicles/${vehicleId}/engine-hours?from=${from}&to=${to}`),
  driverHoursReport: (driverId: string, from: string, to: string) =>
    request<DriverHoursReport>(`/drivers/${driverId}/hours?from=${from}&to=${to}`),

  listDrivers: (params?: ListParams) => request<PageResult<Driver>>(`/drivers${listQuery(params)}`),
  listDriverShiftStatus: () => request<DriverShiftStatus[]>("/drivers/shift-status"),
  createDriver: (body: { tenant_id: string; name: string; license_number?: string; phone?: string; notes?: string }) =>
    request<Driver>("/drivers", { method: "POST", body: JSON.stringify(body) }),
  updateDriver: (id: string, body: Partial<{ name: string; license_number: string; phone: string; notes: string }>) =>
    request<Driver>(`/drivers/${id}`, { method: "PATCH", body: JSON.stringify(body) }),

  listUsers: (params?: ListParams) => request<PageResult<User>>(`/users${listQuery(params)}`),
  createUser: (body: { email: string; password: string; role: TenantRole; tenant_id: string; driver_id?: string }) =>
    request<User>("/users", { method: "POST", body: JSON.stringify(body) }),
  updateUserStatus: (userId: string, status: "active" | "disabled") =>
    request<User>(`/users/${userId}/status`, { method: "PATCH", body: JSON.stringify({ status }) }),
  // Admin password reset -- require_non_driver on the backend with role rules
  // inside (see users.py::reset_user_password).
  resetUserPassword: (userId: string, newPassword: string) =>
    request<User>(`/users/${userId}/reset-password`, {
      method: "POST",
      body: JSON.stringify({ new_password: newPassword }),
    }),
  getUserDeviceAssignments: (userId: string) =>
    request<UserDeviceAssignments>(`/users/${userId}/device-assignments`),
  replaceUserDeviceAssignments: (userId: string, body: UserDeviceAssignments) =>
    request<UserDeviceAssignments>(`/users/${userId}/device-assignments`, {
      method: "PUT",
      body: JSON.stringify(body),
    }),
  getUserNotificationSettings: (userId: string) =>
    request<UserNotificationSettings>(`/users/${userId}/notification-settings`),
  updateUserNotificationSettings: (userId: string, body: Partial<UserNotificationSettings>) =>
    request<UserNotificationSettings>(`/users/${userId}/notification-settings`, {
      method: "PATCH",
      body: JSON.stringify(body),
    }),

  listApiKeys: (userId: string, params?: ListParams) =>
    request<PageResult<ApiKey>>(`/users/${userId}/api-keys${listQuery(params)}`),
  createApiKey: (
    userId: string,
    body: { name: string; can_write: boolean; allowed_device_ids?: string[] | null; expires_in_days?: number }
  ) => request<ApiKeyCreated>(`/users/${userId}/api-keys`, { method: "POST", body: JSON.stringify(body) }),
  revokeApiKey: (userId: string, keyId: string) =>
    request<ApiKey>(`/users/${userId}/api-keys/${keyId}/revoke`, { method: "POST" }),
  listApiKeyUsage: (userId: string, keyId: string, params?: ListParams) =>
    request<PageResult<ApiKeyUsage>>(`/users/${userId}/api-keys/${keyId}/usage${listQuery(params)}`),

  listWebhookEndpoints: (params?: ListParams) =>
    request<PageResult<WebhookEndpoint>>(`/webhook-endpoints${listQuery(params)}`),
  createWebhookEndpoint: (body: { tenant_id: string; url: string; event_types: string[]; enabled?: boolean }) =>
    request<WebhookEndpointCreated>("/webhook-endpoints", { method: "POST", body: JSON.stringify(body) }),
  updateWebhookEndpoint: (id: string, body: Partial<{ url: string; event_types: string[]; enabled: boolean }>) =>
    request<WebhookEndpoint>(`/webhook-endpoints/${id}`, { method: "PATCH", body: JSON.stringify(body) }),
  rotateWebhookSecret: (id: string) =>
    request<WebhookEndpointCreated>(`/webhook-endpoints/${id}/rotate-secret`, { method: "POST" }),
  deleteWebhookEndpoint: (id: string) => request<void>(`/webhook-endpoints/${id}`, { method: "DELETE" }),
  testWebhookEndpoint: (id: string) => request<WebhookTestResult>(`/webhook-endpoints/${id}/test`, { method: "POST" }),
  listWebhookDeliveries: (id: string, params?: ListParams) =>
    request<PageResult<WebhookDelivery>>(`/webhook-endpoints/${id}/deliveries${listQuery(params)}`),

  listDeviceGroups: (params?: ListParams) => request<PageResult<DeviceGroup>>(`/device-groups${listQuery(params)}`),
  createDeviceGroup: (body: { tenant_id: string; name: string }) =>
    request<DeviceGroup>("/device-groups", { method: "POST", body: JSON.stringify(body) }),
  updateDeviceGroup: (id: string, name: string) =>
    request<DeviceGroup>(`/device-groups/${id}`, { method: "PATCH", body: JSON.stringify({ name }) }),
  deleteDeviceGroup: (id: string) => request<void>(`/device-groups/${id}`, { method: "DELETE" }),
  listDeviceGroupMembers: (id: string) => request<string[]>(`/device-groups/${id}/members`),
  replaceDeviceGroupMembers: (id: string, deviceIds: string[]) =>
    request<string[]>(`/device-groups/${id}/members`, {
      method: "PUT",
      body: JSON.stringify({ device_ids: deviceIds }),
    }),

  clockShiftEvent: (eventType: ShiftEventType, coords?: { lat: number; lon: number }) =>
    request<ShiftEvent>("/shifts/clock", {
      method: "POST",
      body: JSON.stringify({ event_type: eventType, lat: coords?.lat, lon: coords?.lon }),
    }),
  listShiftEvents: (params?: ListParams & { driver_id?: string; from?: string; to?: string }) => {
    const q = new URLSearchParams();
    if (params?.search) q.set("search", params.search);
    if (params?.limit != null) q.set("limit", String(params.limit));
    if (params?.offset != null) q.set("offset", String(params.offset));
    if (params?.driver_id) q.set("driver_id", params.driver_id);
    if (params?.from) q.set("from", params.from);
    if (params?.to) q.set("to", params.to);
    const s = q.toString();
    return request<PageResult<ShiftEvent>>(`/shifts${s ? `?${s}` : ""}`);
  },

  // No driver_id filter parameter by design: RLS already narrows a driver token
  // to its own routes (routes_select, migration 0016) -- the same GET serves
  // both "your route today" (DriverHome.tsx) and "all tenant routes"
  // (Dashboard.tsx), depending on the caller.
  listRoutes: (params?: ListParams & { from?: string; to?: string }) => {
    const q = new URLSearchParams();
    if (params?.limit != null) q.set("limit", String(params.limit));
    if (params?.offset != null) q.set("offset", String(params.offset));
    if (params?.from) q.set("from", params.from);
    if (params?.to) q.set("to", params.to);
    if (params?.tenant_id) q.set("tenant_id", params.tenant_id);
    const s = q.toString();
    return request<PageResult<Route>>(`/routes${s ? `?${s}` : ""}`);
  },
  createRoute: (body: { tenant_id: string; name: string; description?: string; date: string; driver_id?: string; vehicle_id?: string }) =>
    request<Route>("/routes", { method: "POST", body: JSON.stringify(body) }),
  updateRoute: (
    id: string,
    body: Partial<{ name: string; description: string; date: string; driver_id: string; vehicle_id: string; status: RouteStatus }>,
  ) => request<Route>(`/routes/${id}`, { method: "PATCH", body: JSON.stringify(body) }),

  requestVideo: (deviceId: string, channel = 1) =>
    request<{ url: string; webrtc_url: string; expires_in_seconds: number; live_view_seconds_remaining: number }>(
      `/devices/${deviceId}/video`,
      { method: "POST", body: JSON.stringify({ channel }) },
    ),
  // Cheap preview photo -- returns an image Blob directly, never JSON. The
  // caller (CameraTile.tsx) creates an object URL with URL.createObjectURL and
  // revokes it when no longer needed.
  requestSnapshot: (deviceId: string, channel = 1) =>
    requestBlob(`/devices/${deviceId}/snapshot`, { method: "POST", body: JSON.stringify({ channel }) }),

  // Live video balance of the unit's tenant + cameras open right now (server
  // central meter, see lib/liveUsage.ts).
  liveViewBalance: (deviceId: string) =>
    request<{ tenant_id: string; remaining_seconds: number; active_sessions: number }>(`/devices/${deviceId}/live-view-balance`),

  latestPositions: () => request<DevicePosition[]>("/positions/latest"),
  devicePositionHistory: (deviceId: string, minutes = 60) =>
    request<DevicePosition[]>(`/devices/${deviceId}/positions?minutes=${minutes}`),
  // Route history -- the result is ALWAYS bounded by the backend (time_bucket()
  // inside Postgres), never relying on the client "not asking for too much".
  deviceRouteHistory: (
    deviceId: string,
    opts: { from: string; to: string; maxPoints?: number; maxEvents?: number },
  ) => {
    const q = new URLSearchParams({ from: opts.from, to: opts.to });
    if (opts.maxPoints != null) q.set("max_points", String(opts.maxPoints));
    if (opts.maxEvents != null) q.set("max_events", String(opts.maxEvents));
    return request<RouteHistoryReport>(`/devices/${deviceId}/route-history?${q.toString()}`);
  },
  // Real-time GPS positions (SSE) -- one-time ticket because EventSource cannot
  // send the Authorization header, so the regular JWT never travels in the
  // stream URL (see api/app/live_positions.py).
  createPositionStreamTicket: () =>
    request<{ ticket: string; expires_in: number }>("/positions/stream/ticket", { method: "POST" }),
  positionStreamUrl: (ticket: string) => `${API_BASE}/positions/stream?ticket=${encodeURIComponent(ticket)}`,

  // In-app mailbox -- same one-time ticket mechanism as positions/stream
  // (EventSource cannot send the Authorization header), see
  // api/app/notifications.py.
  listNotifications: (opts?: { unreadOnly?: boolean; deviceId?: string; limit?: number; offset?: number }) => {
    const q = new URLSearchParams();
    if (opts?.unreadOnly) q.set("unread_only", "true");
    if (opts?.deviceId) q.set("device_id", opts.deviceId);
    if (opts?.limit != null) q.set("limit", String(opts.limit));
    if (opts?.offset != null) q.set("offset", String(opts.offset));
    const s = q.toString();
    return request<NotificationList>(`/notifications${s ? `?${s}` : ""}`);
  },
  markNotificationRead: (id: string) => request<Notification>(`/notifications/${id}/read`, { method: "POST" }),
  createNotificationStreamTicket: () =>
    request<{ ticket: string; expires_in: number }>("/notifications/stream/ticket", { method: "POST" }),
  notificationStreamUrl: (ticket: string) => `${API_BASE}/notifications/stream?ticket=${encodeURIComponent(ticket)}`,

  listAlarms: (opts?: { unacknowledgedOnly?: boolean; deviceId?: string; limit?: number; minSeverity?: AlarmSeverity }) => {
    const q = new URLSearchParams();
    if (opts?.unacknowledgedOnly) q.set("unacknowledged_only", "true");
    if (opts?.minSeverity) q.set("min_severity", opts.minSeverity);
    if (opts?.deviceId) q.set("device_id", opts.deviceId);
    if (opts?.limit != null) q.set("limit", String(opts.limit));
    const s = q.toString();
    return request<Alarm[]>(`/alarms${s ? `?${s}` : ""}`);
  },
  acknowledgeAlarm: (id: string) => request<void>(`/alarms/${id}/acknowledge`, { method: "POST" }),

  requestAlarmClip: (alarmId: string) =>
    request<AlarmVideoClip>(`/alarms/${alarmId}/request-clip`, { method: "POST" }),
  // null when no clip was ever requested for this alarm (backend 404) -- a real,
  // expected state, not an error the caller must handle.
  getAlarmClip: async (alarmId: string): Promise<AlarmVideoClip | null> => {
    try {
      return await request<AlarmVideoClip>(`/alarms/${alarmId}/clip`);
    } catch (err) {
      if (err instanceof ApiError && err.status === 404) return null;
      throw err;
    }
  },

  // Remote commands to devices (engine cut/resume) -- require_tenant_admin on
  // the backend, see device_commands.py.
  sendDeviceCommand: (deviceId: string, commandType: DeviceCommandType) =>
    request<DeviceCommand>(`/devices/${deviceId}/commands`, {
      method: "POST",
      body: JSON.stringify({ command_type: commandType }),
    }),
  // Paginated (Page[T], like listTenants/listDevices/etc.) + date filter.
  // dateFrom/dateTo are full ISO instants (with timezone), NOT bare "YYYY-MM-DD"
  // dates -- the caller (DeviceCommandHistory.tsx) resolves them to the
  // start/end of THAT day in the browser's LOCAL timezone, so "search today"
  // matches the day the user perceives rather than a UTC day that can be hours
  // off.
  listDeviceCommands: (deviceId: string, opts?: { limit?: number; offset?: number; dateFrom?: string; dateTo?: string }) => {
    const q = new URLSearchParams();
    if (opts?.limit != null) q.set("limit", String(opts.limit));
    if (opts?.offset != null) q.set("offset", String(opts.offset));
    if (opts?.dateFrom) q.set("date_from", opts.dateFrom);
    if (opts?.dateTo) q.set("date_to", opts.dateTo);
    const s = q.toString();
    return request<PageResult<DeviceCommand>>(`/devices/${deviceId}/commands${s ? `?${s}` : ""}`);
  },

  // Device health (platform only, migration 0053): deduplicated operational
  // problems with a counter.
  listDeviceHealth: (opts?: { includeResolved?: boolean; limit?: number }) => {
    const q = new URLSearchParams();
    if (opts?.includeResolved) q.set("include_resolved", "true");
    if (opts?.limit) q.set("limit", String(opts.limit));
    const s = q.toString();
    return request<{ open_count: number; items: DeviceHealthEvent[] }>(`/platform/device-health${s ? `?${s}` : ""}`);
  },
  resolveDeviceHealth: (id: string) => request<void>(`/platform/device-health/${id}/resolve`, { method: "POST" }),

  // GT06 CONFIGURATION commands -- require_bypass on the backend
  // (device_config_commands.py), not even tenant_admin.
  sendDeviceConfigCommand: (deviceId: string, commandKey: DeviceConfigCommandKey, params: Record<string, unknown>) =>
    request<DeviceConfigCommand>(`/devices/${deviceId}/config-commands`, {
      method: "POST",
      body: JSON.stringify({ command_key: commandKey, params }),
    }),
  listDeviceConfigCommands: (deviceId: string, opts?: { limit?: number; offset?: number }) => {
    const q = new URLSearchParams();
    if (opts?.limit != null) q.set("limit", String(opts.limit));
    if (opts?.offset != null) q.set("offset", String(opts.offset));
    const s = q.toString();
    return request<PageResult<DeviceConfigCommand>>(`/devices/${deviceId}/config-commands${s ? `?${s}` : ""}`);
  },

  listDriverShiftAlerts: (params?: ListParams & { unacknowledged_only?: boolean }) => {
    const q = new URLSearchParams();
    if (params?.limit != null) q.set("limit", String(params.limit));
    if (params?.offset != null) q.set("offset", String(params.offset));
    if (params?.unacknowledged_only) q.set("unacknowledged_only", "true");
    const s = q.toString();
    return request<PageResult<DriverShiftAlert>>(`/driver-shift-alerts${s ? `?${s}` : ""}`);
  },
  acknowledgeDriverShiftAlert: (id: string) =>
    request<void>(`/driver-shift-alerts/${id}/acknowledge`, { method: "POST" }),

  listBillingPlans: (params?: ListParams & { active_only?: boolean }) => {
    const q = new URLSearchParams();
    if (params?.limit != null) q.set("limit", String(params.limit));
    if (params?.offset != null) q.set("offset", String(params.offset));
    if (params?.active_only) q.set("active_only", "true");
    const s = q.toString();
    return request<PageResult<BillingPlan>>(`/billing/plans${s ? `?${s}` : ""}`);
  },
  createBillingPlan: (body: {
    name: string;
    sku: string;
    category: BillingPlanCategory;
    unit_price: number;
    currency?: string;
    billing_period?: BillingPeriod;
  }) => request<BillingPlan>("/billing/plans", { method: "POST", body: JSON.stringify(body) }),
  updateBillingPlan: (
    id: string,
    body: Partial<{ name: string; unit_price: number; billing_period: BillingPeriod; active: boolean }>,
  ) => request<BillingPlan>(`/billing/plans/${id}`, { method: "PATCH", body: JSON.stringify(body) }),

  listSubscriptionItems: (params?: ListParams & { tenant_id?: string; active_only?: boolean }) => {
    const q = new URLSearchParams();
    if (params?.limit != null) q.set("limit", String(params.limit));
    if (params?.offset != null) q.set("offset", String(params.offset));
    if (params?.tenant_id) q.set("tenant_id", params.tenant_id);
    if (params?.active_only) q.set("active_only", "true");
    const s = q.toString();
    return request<PageResult<TenantSubscriptionItem>>(`/billing/subscription-items${s ? `?${s}` : ""}`);
  },
  // "My billing" -- narrow view of the caller's own subscription, never another
  // tenant's, no tenant_id parameter (always resolves the current session).
  getMySubscription: () => request<TenantSubscriptionItem[]>("/billing/my-subscription"),
  createSubscriptionItem: (body: {
    tenant_id: string;
    billing_plan_id?: string;
    custom_description?: string;
    // Required when there is no billing_plan_id -- see
    // TenantSubscriptionItemCreate in schemas.py.
    category?: BillingPlanCategory;
    quantity?: number;
    unit_price_override?: number;
  }) => request<TenantSubscriptionItem>("/billing/subscription-items", { method: "POST", body: JSON.stringify(body) }),
  updateSubscriptionItem: (
    id: string,
    body: Partial<{ quantity: number; unit_price_override: number; end_now: boolean }>,
  ) => request<TenantSubscriptionItem>(`/billing/subscription-items/${id}`, { method: "PATCH", body: JSON.stringify(body) }),

  listPromotions: (params?: ListParams & { tenant_id?: string }) => {
    const q = new URLSearchParams();
    if (params?.limit != null) q.set("limit", String(params.limit));
    if (params?.offset != null) q.set("offset", String(params.offset));
    if (params?.tenant_id) q.set("tenant_id", params.tenant_id);
    const s = q.toString();
    return request<PageResult<TenantPromotion>>(`/billing/promotions${s ? `?${s}` : ""}`);
  },
  createPromotion: (body: {
    tenant_id: string;
    description: string;
    starts_at: string;
    ends_at: string;
    discount_type: PromotionDiscountType;
    discount_value?: number;
  }) => request<TenantPromotion>("/billing/promotions", { method: "POST", body: JSON.stringify(body) }),

  listInvoices: (params?: ListParams & { tenant_id?: string; status?: InvoiceStatus }) => {
    const q = new URLSearchParams();
    if (params?.limit != null) q.set("limit", String(params.limit));
    if (params?.offset != null) q.set("offset", String(params.offset));
    if (params?.tenant_id) q.set("tenant_id", params.tenant_id);
    if (params?.status) q.set("status", params.status);
    const s = q.toString();
    return request<PageResult<Invoice>>(`/billing/invoices${s ? `?${s}` : ""}`);
  },
  voidInvoice: (id: string) => request<Invoice>(`/billing/invoices/${id}/void`, { method: "POST" }),

  listPayments: (params?: ListParams & { invoice_id?: string; tenant_id?: string }) => {
    const q = new URLSearchParams();
    if (params?.limit != null) q.set("limit", String(params.limit));
    if (params?.offset != null) q.set("offset", String(params.offset));
    if (params?.invoice_id) q.set("invoice_id", params.invoice_id);
    if (params?.tenant_id) q.set("tenant_id", params.tenant_id);
    const s = q.toString();
    return request<PageResult<Payment>>(`/billing/payments${s ? `?${s}` : ""}`);
  },
  createPayment: (body: { invoice_id: string; amount: number; method: PaymentMethod; reference_note?: string }) =>
    request<Payment>("/billing/payments", { method: "POST", body: JSON.stringify(body) }),

  getBillingSettings: () => request<PlatformBillingSettings>("/billing/settings"),
  updateBillingSettings: (
    body: Partial<{
      cost_usd_per_device_month: number;
      cost_usd_per_gps_device_month: number;
      cost_usd_per_gb: number;
      exchange_rate_mxn_per_usd: number;
    }>,
  ) => request<PlatformBillingSettings>("/billing/settings", { method: "PATCH", body: JSON.stringify(body) }),

  listTenantProfitability: (params?: ListParams & { tenant_id?: string }) => {
    const q = new URLSearchParams();
    if (params?.limit != null) q.set("limit", String(params.limit));
    if (params?.offset != null) q.set("offset", String(params.offset));
    if (params?.tenant_id) q.set("tenant_id", params.tenant_id);
    const s = q.toString();
    return request<PageResult<TenantProfitability>>(`/billing/profitability${s ? `?${s}` : ""}`);
  },
  listDeviceDataUsage: (params?: ListParams & { tenant_id?: string }) => {
    const q = new URLSearchParams();
    if (params?.limit != null) q.set("limit", String(params.limit));
    if (params?.offset != null) q.set("offset", String(params.offset));
    if (params?.tenant_id) q.set("tenant_id", params.tenant_id);
    const s = q.toString();
    return request<PageResult<DeviceDataUsage>>(`/billing/sim-usage${s ? `?${s}` : ""}`);
  },

  // Unlike getBillingSettings, readable by ANY authenticated session (see
  // api/app/routers/platform.py) -- a tenant session needs the same threshold as
  // the platform to render its own devices consistently.
  getMonitoringSettings: () => request<PlatformMonitoringSettings>("/platform/monitoring-settings"),
  updateMonitoringSettings: (deviceOfflineThresholdSeconds: number) =>
    request<PlatformMonitoringSettings>("/platform/monitoring-settings", {
      method: "PATCH",
      body: JSON.stringify({ device_offline_threshold_seconds: deviceOfflineThresholdSeconds }),
    }),

  // Like getMonitoringSettings, readable by ANY authenticated session -- all
  // tenants must see the SAME forced map provider, if any. Editing
  // (updateMapSettings) is super_admin-only on the backend, see
  // require_super_admin in platform.py.
  getMapSettings: () => request<PlatformMapSettings>("/platform/map-settings"),
  updateMapSettings: (activeProvider: MapProvider) =>
    request<PlatformMapSettings>("/platform/map-settings", {
      method: "PATCH",
      body: JSON.stringify({ active_provider: activeProvider }),
    }),
  // Geofences -- read: any fleet role; write: tenant_admin or platform (the
  // backend also enforces it in RLS).
  listGeofences: (params?: ListParams & { enabled?: boolean }) => {
    const base = listQuery(params);
    if (params?.enabled == null) return request<PageResult<Geofence>>(`/geofences${base}`);
    const sep = base ? "&" : "?";
    return request<PageResult<Geofence>>(`/geofences${base}${sep}enabled=${params.enabled}`);
  },
  getGeofence: (id: string) => request<Geofence>(`/geofences/${id}`),
  createGeofence: (body: GeofenceCreateBody) =>
    request<Geofence>("/geofences", { method: "POST", body: JSON.stringify(body) }),
  updateGeofence: (id: string, body: GeofenceUpdateBody) =>
    request<Geofence>(`/geofences/${id}`, { method: "PATCH", body: JSON.stringify(body) }),
  deleteGeofence: (id: string) => request<void>(`/geofences/${id}`, { method: "DELETE" }),
  geofenceOccupancy: (id: string) => request<GeofenceOccupant[]>(`/geofences/${id}/occupancy`),
  listGeofenceEvents: (q: GeofenceEventQuery) => {
    const params = new URLSearchParams({ from: q.from, to: q.to });
    if (q.deviceId) params.set("device_id", q.deviceId);
    if (q.geofenceId) params.set("geofence_id", q.geofenceId);
    if (q.eventType) params.set("event_type", q.eventType);
    if (q.limit != null) params.set("limit", String(q.limit));
    if (q.offset != null) params.set("offset", String(q.offset));
    return request<PageResult<GeofenceEvent>>(`/geofences/events?${params.toString()}`);
  },
  geofenceReport: (q: { from: string; to: string; deviceId?: string; geofenceId?: string }) => {
    const params = new URLSearchParams({ from: q.from, to: q.to });
    if (q.deviceId) params.set("device_id", q.deviceId);
    if (q.geofenceId) params.set("geofence_id", q.geofenceId);
    return request<GeofenceReport>(`/geofences/report?${params.toString()}`);
  },
};
