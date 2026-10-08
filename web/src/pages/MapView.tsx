import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { Link, useSearchParams } from "react-router-dom";
import L from "leaflet";
import "leaflet/dist/leaflet.css";
import "leaflet.markercluster/dist/MarkerCluster.css";
import "leaflet.markercluster/dist/MarkerCluster.Default.css";
import { MapContainer, Marker, Polyline, Popup, useMap } from "react-leaflet";
import MarkerClusterGroup from "react-leaflet-cluster";
import { api, hasCamera, type DevicePosition, type Geofence } from "../lib/api";
import { Alert } from "../components/ui";
import { DeviceDetailPanel } from "../components/DeviceDetailPanel";
import { DeviceListPanel, StatusAvatar, useUnitRows, type UnitRow } from "../components/DeviceListPanel";
import { useFleetRoster, type LiveConnectionState } from "../lib/useFleetRoster";
import { useIsMobile } from "../lib/useIsMobile";
import { useGeofenceOverlay } from "../lib/useGeofenceOverlay";
import { GeofenceLayer } from "../components/GeofenceLayer";
import { CameraIcon, CloseIcon, GeofenceIcon, LocateIcon, MinusIcon, PanelCloseIcon, PanelOpenIcon, PlusIcon, RefreshIcon, SearchIcon } from "../components/icons";
import { MapBaseLayer, MapStyleSwitcher } from "../components/MapBaseLayer";
import { UNIT_STATUS_META, lastSeenLabel, useDeviceOfflineThreshold, type UnitStatus } from "../lib/deviceStatus";
import { useFloatingCameras } from "../lib/floatingCameras";

// Main map. Decisions:
// - Full-screen map as the protagonist; on desktop the list and detail are
//   FLOATING glass panels over the map instead of columns stealing map width.
// - Markers colored by status (moving / idling / stopped / no signal / alarm),
//   with a heading arrow and a readable label.
// - Fleet summary on top that also filters the markers.
// - Cameras in global floating windows/dock (lib/floatingCameras.tsx). No
//   floating layer uses position:fixed: everything is absolute inside the page's
//   relative container, and each Leaflet map has its own stacking context
//   (.leaflet-container isolation:isolate in index.css).

const MEXICO_CITY: [number, number] = [19.4326, -99.1332];

const CONNECTION_LABEL: Record<LiveConnectionState, string> = {
  connecting: "Conectando…",
  open: "En vivo",
  reconnecting: "Reconectando…",
};

// Icons cached by their visual key: with hundreds of units and positions
// arriving over SSE, recreating a DivIcon per render forced Leaflet to replace
// each marker's DOM even when nothing visible changed.
const iconCache = new Map<string, L.DivIcon>();

function deviceIcon(status: UnitStatus, heading: number | null, label: string, selected: boolean): L.DivIcon {
  const h = heading == null || status === "offline" ? null : Math.round(heading / 10) * 10;
  const key = `${status}|${h}|${label}|${selected}`;
  const cached = iconCache.get(key);
  if (cached) return cached;
  const color = UNIT_STATUS_META[status].color;
  const safeLabel = label.replace(/[&<>"']/g, (c) => `&#${c.charCodeAt(0)};`);
  const arrow = h == null ? "" : `<div class="omd-marker-arrow" style="transform:rotate(${h}deg)"></div>`;
  const icon = L.divIcon({
    className: "",
    html: `<div class="omd-marker${selected ? " is-selected" : ""}"><div class="omd-marker-dot${status === "alarm" ? " omd-pulse" : ""}" style="background:${color}">${arrow}</div><div class="omd-marker-label">${safeLabel}</div></div>`,
    iconSize: [22, 22],
    iconAnchor: [11, 11],
  });
  iconCache.set(key, icon);
  return icon;
}

function checkinPointIcon(): L.DivIcon {
  return L.divIcon({
    className: "",
    html: `<div style="background:#a855f7;width:18px;height:18px;border-radius:50% 50% 50% 0;transform:rotate(-45deg);border:2.5px solid white;box-shadow:0 2px 8px rgba(0,0,0,.5);"></div>`,
    iconSize: [18, 18],
    iconAnchor: [9, 18],
  });
}

function ControlButton({ label, onClick, active = false, children }: { label: string; onClick: () => void; active?: boolean; children: ReactNode }) {
  return (
    <button
      type="button"
      onClick={onClick}
      title={label}
      aria-label={label}
      aria-pressed={active}
      className={`flex h-9 w-9 items-center justify-center transition-colors hover:bg-fg/10 ${active ? "text-brand-400" : "text-ink"}`}
    >
      {children}
    </button>
  );
}

// Floating controls (inside MapContainer to use useMap()).
// disableClickPropagation: a tap on a control never reaches the map.
function MapControls({
  positions,
  onRefresh,
  geofenceCount,
  geofencesVisible,
  onToggleGeofences,
  className,
}: {
  positions: DevicePosition[];
  onRefresh: () => void;
  geofenceCount: number;
  geofencesVisible: boolean;
  onToggleGeofences: () => void;
  className: string;
}) {
  const map = useMap();
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!ref.current) return;
    L.DomEvent.disableClickPropagation(ref.current);
    L.DomEvent.disableScrollPropagation(ref.current);
  }, []);

  function fitAll() {
    if (positions.length === 0) return;
    map.flyToBounds(positions.map((p): [number, number] => [p.lat, p.lon]), { padding: [60, 60], maxZoom: 15, duration: 0.7 });
  }

  return (
    <div ref={ref} className={`absolute z-[1000] flex flex-col items-end gap-2 transition-[right] duration-200 ${className}`}>
      <MapStyleSwitcher className="relative" />
      <div className="glass flex flex-col divide-y divide-fg/[0.06] overflow-hidden rounded-xl">
        <ControlButton label="Acercar" onClick={() => map.zoomIn()}>
          <PlusIcon />
        </ControlButton>
        <ControlButton label="Alejar" onClick={() => map.zoomOut()}>
          <MinusIcon />
        </ControlButton>
        <ControlButton label="Ver toda la flota" onClick={fitAll}>
          <LocateIcon />
        </ControlButton>
        {geofenceCount > 0 && (
          <ControlButton label={geofencesVisible ? "Ocultar geocercas" : "Mostrar geocercas"} onClick={onToggleGeofences} active={geofencesVisible}>
            <GeofenceIcon />
          </ControlButton>
        )}
        <ControlButton label="Refrescar flota" onClick={onRefresh}>
          <RefreshIcon />
        </ControlButton>
      </div>
    </div>
  );
}

// New selection -> flyTo with zoom; same unit moving -> panTo without touching
// zoom (follow the vehicle without fighting the user's zoom).
function MapFlyTo({ selectedId, position }: { selectedId: string | null; position: DevicePosition | null }) {
  const map = useMap();
  const lastSelectedIdRef = useRef<string | null>(null);
  useEffect(() => {
    if (!position) return;
    const isNewSelection = lastSelectedIdRef.current !== selectedId;
    lastSelectedIdRef.current = selectedId;
    if (isNewSelection) map.flyTo([position.lat, position.lon], Math.max(map.getZoom(), 15), { duration: 0.8 });
    else map.panTo([position.lat, position.lon], { animate: true, duration: 0.5 });
  }, [selectedId, position, map]);
  return null;
}

// Fits the whole fleet ONCE on entry (never moves the map afterwards); skipped
// when opened with a preselected unit or point.
function InitialFitBounds({ positions, skip }: { positions: DevicePosition[]; skip: boolean }) {
  const map = useMap();
  const attemptedRef = useRef(false);
  useEffect(() => {
    if (attemptedRef.current || positions.length === 0) return;
    attemptedRef.current = true;
    if (skip) return;
    if (positions.length === 1) {
      map.setView([positions[0].lat, positions[0].lon], 14);
      return;
    }
    map.fitBounds(positions.map((p): [number, number] => [p.lat, p.lon]), { padding: [60, 60], maxZoom: 15 });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [positions]);
  return null;
}

export interface CheckinPoint {
  lat: number;
  lon: number;
  label: string;
}

// Historical point (where a driver clocked in, from Operations) -- a distinct
// icon so it is never confused with a live position.
function CheckinPointView({ point }: { point: CheckinPoint | null }) {
  const map = useMap();
  const firedRef = useRef(false);
  useEffect(() => {
    if (!point || firedRef.current) return;
    firedRef.current = true;
    map.setView([point.lat, point.lon], 15);
  }, [point, map]);
  if (!point) return null;
  return (
    <Marker position={[point.lat, point.lon]} icon={checkinPointIcon()}>
      <Popup>
        <strong>{point.label}</strong>
        <br />
        Punto histórico de check-in, no es una posición en vivo.
      </Popup>
    </Marker>
  );
}

// Fleet summary -- counts per status; tapping one filters the markers.
function FleetSummary({
  rows,
  statusFilter,
  onFilter,
  connectionState,
}: {
  rows: UnitRow[];
  statusFilter: UnitStatus | null;
  onFilter: (s: UnitStatus | null) => void;
  connectionState: LiveConnectionState;
}) {
  const counts = useMemo(() => {
    const c: Partial<Record<UnitStatus, number>> = {};
    rows.forEach((r) => (c[r.status] = (c[r.status] ?? 0) + 1));
    return c;
  }, [rows]);
  const order: UnitStatus[] = ["moving", "idle", "stopped", "offline", "alarm"];
  return (
    <div className="glass flex max-w-full items-center gap-1 overflow-x-auto rounded-2xl p-1 no-scrollbar">
      <span className="flex shrink-0 items-center gap-1.5 px-2.5 text-[11px] font-semibold text-ink-dim" title={CONNECTION_LABEL[connectionState]}>
        <span className={`h-2 w-2 rounded-full ${connectionState === "open" ? "bg-emerald-400" : "animate-pulse bg-amber-400"}`} aria-hidden />
        <span className="hidden sm:inline">{CONNECTION_LABEL[connectionState]}</span>
      </span>
      {order.map((s) =>
        counts[s] ? (
          <button
            key={s}
            onClick={() => onFilter(statusFilter === s ? null : s)}
            className={`flex shrink-0 items-center gap-1.5 rounded-xl px-2.5 py-1.5 text-xs font-medium transition-colors ${
              statusFilter === s ? "bg-fg/[0.12] text-ink" : statusFilter ? "text-ink-faint hover:text-ink" : "text-ink-dim hover:bg-fg/[0.06] hover:text-ink"
            }`}
            title={`Mostrar solo: ${UNIT_STATUS_META[s].label}`}
          >
            <span className="h-2 w-2 rounded-full" style={{ background: UNIT_STATUS_META[s].color }} aria-hidden />
            <span className="font-data font-semibold text-ink">{counts[s]}</span>
            <span className="hidden md:inline">{UNIT_STATUS_META[s].label}</span>
          </button>
        ) : null,
      )}
    </div>
  );
}

function MapCanvas({
  center,
  rows,
  positions,
  selectedId,
  onSelect,
  trail,
  checkinPoint,
  geofences,
  geofencesVisible,
  onToggleGeofences,
  onRefresh,
  controlsClassName,
}: {
  center: [number, number];
  rows: UnitRow[];
  positions: DevicePosition[];
  selectedId: string | null;
  onSelect: (id: string) => void;
  trail: DevicePosition[] | null;
  checkinPoint: CheckinPoint | null;
  geofences: Geofence[];
  geofencesVisible: boolean;
  onToggleGeofences: () => void;
  onRefresh: () => void;
  controlsClassName: string;
}) {
  const selectedPosition = rows.find((r) => r.device.id === selectedId)?.position ?? null;
  return (
    <MapContainer center={center} zoom={5} minZoom={3} maxZoom={19} zoomControl={false} className="absolute inset-0 h-full w-full">
      <MapBaseLayer />
      <MapControls
        positions={positions}
        onRefresh={onRefresh}
        geofenceCount={geofences.length}
        geofencesVisible={geofencesVisible}
        onToggleGeofences={onToggleGeofences}
        className={controlsClassName}
      />
      {geofencesVisible && <GeofenceLayer geofences={geofences} permanentLabels={geofences.length <= 15} />}
      <MapFlyTo selectedId={selectedId} position={selectedPosition} />
      <InitialFitBounds positions={positions} skip={selectedId != null || checkinPoint != null} />
      <CheckinPointView point={checkinPoint} />
      {trail && trail.length > 1 && (
        <Polyline positions={trail.map((p): [number, number] => [p.lat, p.lon])} pathOptions={{ color: "#2f93ff", weight: 4, opacity: 0.9 }} />
      )}
      <MarkerClusterGroup chunkedLoading maxClusterRadius={50} showCoverageOnHover={false}>
        {rows
          .filter((r) => r.position)
          .map((r) => (
            <Marker
              key={r.device.id}
              position={[r.position!.lat, r.position!.lon]}
              icon={deviceIcon(r.status, r.position!.heading, r.device.label, r.device.id === selectedId)}
              zIndexOffset={r.device.id === selectedId ? 1000 : r.status === "alarm" ? 500 : 0}
              eventHandlers={{ click: () => onSelect(r.device.id) }}
            />
          ))}
      </MarkerClusterGroup>
    </MapContainer>
  );
}

// Selected unit card (mobile) -- absolute inside the map container, never fixed.
function MobileUnitCard({ row, onClose, trailVisible, onToggleTrail }: { row: UnitRow; onClose: () => void; trailVisible: boolean; onToggleTrail: () => void }) {
  const threshold = useDeviceOfflineThreshold();
  const { openCamera } = useFloatingCameras();
  const d = row.device;
  const camChannels = !hasCamera(d.protocol) ? [] : d.protocol === "gt06_video" ? [0, 1] : [undefined];
  return (
    <div className="glass rounded-3xl p-3.5">
      <div className="flex items-start gap-3">
        <StatusAvatar status={row.status} size={42} />
        <div className="min-w-0 flex-1">
          <p className="truncate text-[15px] font-semibold text-ink">{d.label}</p>
          <p className="truncate text-xs text-ink-dim">
            {UNIT_STATUS_META[row.status].label}
            {row.liveSpeed != null && row.status === "moving" ? ` · ${Math.round(row.liveSpeed)} km/h` : ` · ${lastSeenLabel(d.last_seen_at, threshold)}`}
          </p>
          {row.vehicle && (
            <p className="truncate text-[11px] text-ink-faint">{[row.vehicle.plate, row.vehicle.current_driver_name].filter(Boolean).join(" · ")}</p>
          )}
        </div>
        <button onClick={onClose} aria-label="Cerrar" className="flex h-8 w-8 items-center justify-center rounded-full bg-fg/[0.08] text-ink-dim">
          <CloseIcon size={14} />
        </button>
      </div>
      <div className="mt-3 grid grid-cols-3 gap-2">
        <Link to={`/units/${d.id}`} className="rounded-2xl bg-brand-600 py-2.5 text-center text-xs font-semibold text-white">
          Detalle
        </Link>
        <button
          disabled={camChannels.length === 0}
          onClick={() =>
            camChannels.forEach((ch) =>
              openCamera({ deviceId: d.id, channel: ch, protocol: d.protocol, label: ch === 0 ? `${d.label} · Frontal` : ch === 1 ? `${d.label} · Cabina` : d.label }),
            )
          }
          className="flex items-center justify-center gap-1.5 rounded-2xl bg-fg/[0.08] py-2.5 text-xs font-semibold text-ink disabled:opacity-40"
        >
          <CameraIcon size={14} /> Cámara
        </button>
        <button
          disabled={!row.position}
          onClick={onToggleTrail}
          className={`rounded-2xl py-2.5 text-xs font-semibold disabled:opacity-40 ${trailVisible ? "bg-brand-600/20 text-brand-300" : "bg-fg/[0.08] text-ink"}`}
        >
          {trailVisible ? "Ocultar ruta" : "Última hora"}
        </button>
      </div>
    </div>
  );
}

export default function MapView() {
  const isMobile = useIsMobile();
  const { devices, positions, connectionState, error, refresh, severityByDevice, positionByDevice, tenantById, vehicleById } = useFleetRoster();
  const geofenceOverlay = useGeofenceOverlay();
  const { openCamera } = useFloatingCameras();
  const [searchParams] = useSearchParams();
  const [selectedId, setSelectedId] = useState<string | null>(() => searchParams.get("device") || null);
  const [trail, setTrail] = useState<DevicePosition[] | null>(null);
  const [statusFilter, setStatusFilter] = useState<UnitStatus | null>(null);
  const [listOpen, setListOpen] = useState(true);
  // ?lat=&lon=&label= -- historical point (Operations, "view on map").
  const [checkinPoint] = useState<CheckinPoint | null>(() => {
    const lat = Number(searchParams.get("lat"));
    const lon = Number(searchParams.get("lon"));
    if (searchParams.get("lat") == null || !Number.isFinite(lat) || !Number.isFinite(lon)) return null;
    return { lat, lon, label: searchParams.get("label") ?? "Ubicación" };
  });

  const rows = useUnitRows(devices, vehicleById, positionByDevice, severityByDevice);
  const visibleRows = useMemo(
    () => (statusFilter ? rows.filter((r) => r.status === statusFilter || r.device.id === selectedId) : rows),
    [rows, statusFilter, selectedId],
  );
  const selectedRow = rows.find((r) => r.device.id === selectedId) ?? null;

  async function loadTrail(deviceId: string) {
    try {
      setTrail(await api.devicePositionHistory(deviceId, 60));
    } catch {
      // a failed trail must not cover the rest of the screen
    }
  }

  // ?trail=1 (from the unit card, "last hour") and ?addToTray= (legacy tray
  // links, now open the floating window) -- once, when the roster has loaded.
  const initialParamsHandled = useRef(false);
  useEffect(() => {
    if (initialParamsHandled.current || devices.length === 0) return;
    initialParamsHandled.current = true;
    const dev = searchParams.get("device");
    if (dev && searchParams.get("trail") === "1") loadTrail(dev);
    const addId = searchParams.get("addToTray");
    const target = addId ? devices.find((d) => d.id === addId) : undefined;
    if (target) {
      const ch = searchParams.get("channel");
      openCamera({ deviceId: target.id, channel: ch != null ? Number(ch) : undefined, protocol: target.protocol, label: target.label });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [devices]);

  function selectDevice(id: string) {
    setSelectedId(id);
    setTrail(null);
  }
  function closeSelection() {
    setSelectedId(null);
    setTrail(null);
  }
  function toggleTrail() {
    if (!selectedId) return;
    if (trail) setTrail(null);
    else loadTrail(selectedId);
  }

  const center: [number, number] = checkinPoint
    ? [checkinPoint.lat, checkinPoint.lon]
    : selectedRow?.position
      ? [selectedRow.position.lat, selectedRow.position.lon]
      : positions.length > 0
        ? [positions[0].lat, positions[0].lon]
        : MEXICO_CITY;

  const canvas = (controlsClassName: string) => (
    <MapCanvas
      center={center}
      rows={visibleRows}
      positions={positions}
      selectedId={selectedId}
      onSelect={selectDevice}
      trail={trail}
      checkinPoint={checkinPoint}
      geofences={geofenceOverlay.geofences}
      geofencesVisible={geofenceOverlay.visible}
      onToggleGeofences={geofenceOverlay.toggle}
      onRefresh={refresh}
      controlsClassName={controlsClassName}
    />
  );

  if (isMobile) {
    return (
      <div className="relative h-full w-full overflow-hidden">
        {canvas("top-[124px] right-3")}
        <div className="pointer-events-none absolute inset-x-3 top-2 z-[1100] flex flex-col gap-2">
          <div className="pointer-events-auto flex items-center gap-2">
            <Link to="/units" className="glass flex h-11 min-w-0 flex-1 items-center gap-2 rounded-2xl px-3.5 text-sm text-ink-dim">
              <SearchIcon />
              <span className="truncate">Buscar entre {devices.length} unidades</span>
            </Link>
          </div>
          <div className="pointer-events-auto">
            <FleetSummary rows={rows} statusFilter={statusFilter} onFilter={setStatusFilter} connectionState={connectionState} />
          </div>
          {error && (
            <div className="pointer-events-auto">
              <Alert>{error}</Alert>
            </div>
          )}
        </div>
        {selectedRow && (
          <div className="absolute inset-x-3 bottom-3 z-[1100]">
            <MobileUnitCard row={selectedRow} onClose={closeSelection} trailVisible={trail !== null} onToggleTrail={toggleTrail} />
          </div>
        )}
      </div>
    );
  }

  return (
    <div className="relative h-full w-full overflow-hidden">
      {canvas(selectedRow ? "top-3 right-[404px]" : "top-3 right-3")}

      {/* Unit list (left floating panel, collapsible). */}
      {listOpen ? (
        <aside className="glass absolute top-3 bottom-3 left-3 z-[1100] flex w-[340px] flex-col overflow-hidden rounded-3xl">
          <div className="flex items-center justify-between px-4 pt-3.5">
            <h2 className="text-[15px] font-semibold tracking-tight text-ink">
              Unidades <span className="font-data text-ink-faint">{devices.length}</span>
            </h2>
            <button
              onClick={() => setListOpen(false)}
              aria-label="Ocultar lista"
              title="Ocultar lista"
              className="flex h-8 w-8 items-center justify-center rounded-lg text-ink-dim hover:bg-fg/[0.08] hover:text-ink"
            >
              <PanelCloseIcon />
            </button>
          </div>
          <div className="min-h-0 flex-1">
            <DeviceListPanel
              devices={devices}
              vehicleById={vehicleById}
              positionByDevice={positionByDevice}
              severityByDevice={severityByDevice}
              selectedId={selectedId}
              onSelect={selectDevice}
            />
          </div>
        </aside>
      ) : (
        <button
          onClick={() => setListOpen(true)}
          className="glass absolute top-3 left-3 z-[1100] flex h-11 items-center gap-2 rounded-2xl px-3.5 text-sm font-semibold text-ink"
        >
          <PanelOpenIcon />
          Unidades <span className="font-data text-ink-faint">{devices.length}</span>
        </button>
      )}

      {/* Fleet summary, centered on top between both panels. */}
      <div
        className="pointer-events-none absolute top-3 z-[1050] flex justify-center"
        style={{ left: listOpen ? 360 : 180, right: selectedRow ? 460 : 70 }}
      >
        <div className="pointer-events-auto max-w-full">
          <FleetSummary rows={rows} statusFilter={statusFilter} onFilter={setStatusFilter} connectionState={connectionState} />
        </div>
      </div>

      {error && (
        <div className="absolute bottom-6 left-1/2 z-[1100] w-[min(520px,60%)] -translate-x-1/2">
          <Alert>{error}</Alert>
        </div>
      )}

      {/* Detail (right floating panel). */}
      {selectedRow && (
        <aside className="glass absolute top-3 right-3 bottom-3 z-[1100] w-[380px] overflow-hidden rounded-3xl">
          <DeviceDetailPanel
            device={selectedRow.device}
            vehicle={selectedRow.vehicle ?? null}
            position={selectedRow.position ?? null}
            tenant={tenantById.get(selectedRow.device.tenant_id) ?? null}
            hasAlarm={severityByDevice.has(selectedRow.device.id)}
            trailVisible={trail !== null}
            onToggleTrail={toggleTrail}
            onClose={closeSelection}
          />
        </aside>
      )}
    </div>
  );
}
