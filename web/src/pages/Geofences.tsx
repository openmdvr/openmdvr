import { useEffect, useMemo, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import L from "leaflet";
import "leaflet/dist/leaflet.css";
import { Circle, CircleMarker, MapContainer, Marker, Polygon, Polyline, Tooltip, useMap, useMapEvents } from "react-leaflet";
import {
  api,
  ApiError,
  type AlarmSeverity,
  type Device,
  type DevicePosition,
  type Geofence,
  type GeofenceCreateBody,
  type GeofenceEvent,
  type GeofenceOccupant,
  type GeofenceShape,
  type LatLonTuple,
  type Tenant,
} from "../lib/api";
import { useAuth } from "../lib/auth";
import { MapBaseLayer, MapStyleSwitcher } from "../components/MapBaseLayer";
import {
  GEOFENCE_EVENT_LABEL,
  GEOFENCE_PALETTE,
  SEVERITY_LABEL,
  formatDuration,
  geofenceBounds,
  geofenceSizeLabel,
  polygonAreaM2,
  formatArea,
} from "../lib/geofences";
import { GeofenceLayer } from "../components/GeofenceLayer";
import { Alert, Badge, Button, Card, CardTitle, EmptyState, Field, Input, PageContainer, PageHeader, Select, type BadgeTone } from "../components/ui";

// Geofence module: map boundaries that generate events depending on
// configuration (enter, exit, dwell). The real evaluation lives in the database
// (0052, insert_gps_position) -- this page only manages and visualizes.
//
// Mobile-first layout WITHOUT position:fixed: on a phone the map goes on top and
// the panel (list or editor) below, in normal flow; on large screens, panel on
// the left and full-height map on the right.
//
// Drawing by TAP (not drag): a tap sets the circle center or adds a polygon
// vertex -- identical with finger or mouse, no gestures fighting the map's
// panning on a phone.

const MEXICO_CITY: LatLonTuple = [19.4326, -99.1332];
const DEFAULT_RADIUS_M = 300;

const SEVERITY_TONE: Record<AlarmSeverity, BadgeTone> = { info: "brand", warning: "warning", critical: "danger" };
const EVENT_TONE: Record<GeofenceEvent["event_type"], BadgeTone> = { enter: "success", exit: "neutral", dwell: "warning" };

interface Draft {
  id: string | null;
  tenant_id: string;
  name: string;
  description: string;
  color: string;
  shape: GeofenceShape;
  center: LatLonTuple | null;
  radius_m: number;
  polygon: LatLonTuple[];
  enabled: boolean;
  notify_on_enter: boolean;
  notify_on_exit: boolean;
  dwell_enabled: boolean;
  dwell_minutes: number;
  severity: AlarmSeverity;
  hysteresis_m: number;
  applies_to_all_devices: boolean;
  device_ids: string[];
}

function emptyDraft(tenantId: string, colorIndex: number): Draft {
  return {
    id: null,
    tenant_id: tenantId,
    name: "",
    description: "",
    color: GEOFENCE_PALETTE[colorIndex % GEOFENCE_PALETTE.length],
    shape: "circle",
    center: null,
    radius_m: DEFAULT_RADIUS_M,
    polygon: [],
    enabled: true,
    notify_on_enter: true,
    notify_on_exit: true,
    dwell_enabled: false,
    dwell_minutes: 30,
    severity: "info",
    hysteresis_m: 20,
    applies_to_all_devices: true,
    device_ids: [],
  };
}

function draftFromGeofence(g: Geofence): Draft {
  return {
    id: g.id,
    tenant_id: g.tenant_id,
    name: g.name,
    description: g.description ?? "",
    color: g.color,
    shape: g.shape,
    center: g.center_lat != null && g.center_lon != null ? [g.center_lat, g.center_lon] : null,
    radius_m: g.radius_m ?? DEFAULT_RADIUS_M,
    polygon: g.polygon ?? [],
    enabled: g.enabled,
    notify_on_enter: g.notify_on_enter,
    notify_on_exit: g.notify_on_exit,
    dwell_enabled: g.dwell_minutes != null,
    dwell_minutes: g.dwell_minutes ?? 30,
    severity: g.severity,
    hysteresis_m: g.hysteresis_m,
    applies_to_all_devices: g.applies_to_all_devices,
    device_ids: g.device_ids,
  };
}

function geometryReady(d: Draft): boolean {
  return d.shape === "circle" ? d.center != null && d.radius_m >= 10 : d.polygon.length >= 3;
}

function vertexIcon(color: string, first: boolean): L.DivIcon {
  const size = first ? 16 : 12;
  return L.divIcon({
    className: "",
    html: `<div style="width:${size}px;height:${size}px;border-radius:9999px;background:${first ? color : "#fff"};border:2px solid ${color};box-shadow:0 0 0 1px rgba(0,0,0,.35)"></div>`,
    iconSize: [size, size],
    iconAnchor: [size / 2, size / 2],
  });
}

// Fits the existing geofences ONCE on load (same as MapView.tsx's
// InitialFitBounds): never moves the map again while the user works.
function FitOnce({ points, fallback }: { points: LatLonTuple[]; fallback: LatLonTuple[] }) {
  const map = useMap();
  const [done, setDone] = useState(false);
  useEffect(() => {
    if (done) return;
    const target = points.length > 0 ? points : fallback;
    if (target.length === 0) return;
    if (target.length === 1) map.setView(target[0], 14);
    else map.fitBounds(target, { padding: [40, 40], maxZoom: 16 });
    setDone(true);
  }, [points, fallback, done, map]);
  return null;
}

function FlyToBounds({ target }: { target: { key: number; points: LatLonTuple[] } | null }) {
  const map = useMap();
  useEffect(() => {
    if (!target || target.points.length === 0) return;
    map.flyToBounds(target.points, { padding: [50, 50], maxZoom: 17, duration: 0.6 });
  }, [target, map]);
  return null;
}

function DrawHandler({ draft, onChange }: { draft: Draft | null; onChange: (d: Draft) => void }) {
  useMapEvents({
    click(e) {
      if (!draft) return;
      const point: LatLonTuple = [e.latlng.lat, e.latlng.lng];
      if (draft.shape === "circle") onChange({ ...draft, center: point });
      else if (draft.polygon.length < 500) onChange({ ...draft, polygon: [...draft.polygon, point] });
    },
  });
  return null;
}

function DraftShape({ draft, onChange }: { draft: Draft; onChange: (d: Draft) => void }) {
  const pathOptions = { color: draft.color, weight: 2, fillColor: draft.color, fillOpacity: 0.22, dashArray: "5 5" };
  if (draft.shape === "circle") {
    if (!draft.center) return null;
    return (
      <>
        <Circle center={draft.center} radius={draft.radius_m} pathOptions={pathOptions} interactive={false} />
        <Marker
          position={draft.center}
          icon={vertexIcon(draft.color, true)}
          draggable
          eventHandlers={{
            dragend: (e) => {
              const ll = (e.target as L.Marker).getLatLng();
              onChange({ ...draft, center: [ll.lat, ll.lng] });
            },
          }}
        />
      </>
    );
  }
  return (
    <>
      {draft.polygon.length >= 3 ? (
        <Polygon positions={draft.polygon} pathOptions={pathOptions} interactive={false} />
      ) : draft.polygon.length === 2 ? (
        <Polyline positions={draft.polygon} pathOptions={pathOptions} interactive={false} />
      ) : null}
      {draft.polygon.map((p, i) => (
        <Marker
          key={i}
          position={p}
          icon={vertexIcon(draft.color, i === 0)}
          draggable
          eventHandlers={{
            dragend: (e) => {
              const ll = (e.target as L.Marker).getLatLng();
              const next = draft.polygon.slice();
              next[i] = [ll.lat, ll.lng];
              onChange({ ...draft, polygon: next });
            },
          }}
        />
      ))}
    </>
  );
}

function Toggle({ checked, onChange, label, hint }: { checked: boolean; onChange: (v: boolean) => void; label: string; hint?: string }) {
  return (
    <label className="flex cursor-pointer items-start gap-3 rounded-sm border border-line-strong bg-surface-2 px-3 py-2.5">
      <input type="checkbox" className="mt-0.5 h-4 w-4 accent-brand-600" checked={checked} onChange={(e) => onChange(e.target.checked)} />
      <span className="min-w-0">
        <span className="block text-sm font-medium text-ink">{label}</span>
        {hint && <span className="block text-xs text-ink-dim">{hint}</span>}
      </span>
    </label>
  );
}

export default function Geofences() {
  const { role, isPlatform, tenantId: ownTenantId } = useAuth();
  const canManage = isPlatform || role === "tenant_admin";
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();

  const [geofences, setGeofences] = useState<Geofence[]>([]);
  const [devices, setDevices] = useState<Device[]>([]);
  const [positions, setPositions] = useState<DevicePosition[]>([]);
  const [tenants, setTenants] = useState<Tenant[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [search, setSearch] = useState("");

  const selectedId = searchParams.get("id");
  const [draft, setDraft] = useState<Draft | null>(null);
  const [saving, setSaving] = useState(false);
  const [deleteArmed, setDeleteArmed] = useState(false);
  const [deviceFilter, setDeviceFilter] = useState("");
  const [flyTarget, setFlyTarget] = useState<{ key: number; points: LatLonTuple[] } | null>(null);

  const [occupants, setOccupants] = useState<GeofenceOccupant[]>([]);
  const [recentEvents, setRecentEvents] = useState<GeofenceEvent[]>([]);

  async function reload() {
    const page = await api.listGeofences({ limit: 500 });
    setGeofences(page.items);
    return page.items;
  }

  useEffect(() => {
    Promise.all([
      reload(),
      api.listDevices({ limit: 1000, exclude_inactive: true }).then((p) => setDevices(p.items)),
      api.latestPositions().then(setPositions),
      isPlatform ? api.listTenants({ limit: 1000 }).then((p) => setTenants(p.items)) : Promise.resolve(),
    ])
      .catch((err) => setError(err instanceof ApiError ? err.message : "error cargando geocercas"))
      .finally(() => setLoaded(true));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const selected = useMemo(() => geofences.find((g) => g.id === selectedId) ?? null, [geofences, selectedId]);
  const tenantName = useMemo(() => new Map(tenants.map((t) => [t.id, t.name])), [tenants]);

  // Detail of the selected geofence: who is inside now + latest events (24 h).
  // Queried only on selection, never polled.
  useEffect(() => {
    if (!selected || draft) return;
    let cancelled = false;
    const now = new Date();
    Promise.all([
      api.geofenceOccupancy(selected.id),
      api.listGeofenceEvents({
        from: new Date(now.getTime() - 24 * 3600 * 1000).toISOString(),
        to: new Date(now.getTime() + 60 * 1000).toISOString(),
        geofenceId: selected.id,
        limit: 20,
      }),
    ])
      .then(([occ, ev]) => {
        if (cancelled) return;
        setOccupants(occ);
        setRecentEvents(ev.items);
      })
      .catch(() => {
        if (!cancelled) {
          setOccupants([]);
          setRecentEvents([]);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [selected, draft]);

  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase();
    return q ? geofences.filter((g) => g.name.toLowerCase().includes(q)) : geofences;
  }, [geofences, search]);

  const allFencePoints = useMemo(() => geofences.flatMap((g) => geofenceBounds(g)), [geofences]);
  const positionPoints = useMemo(() => positions.map((p): LatLonTuple => [p.lat, p.lon]), [positions]);

  const draftTenantDevices = useMemo(
    () => (draft ? devices.filter((d) => d.tenant_id === draft.tenant_id) : []),
    [devices, draft],
  );

  function select(g: Geofence | null) {
    setDraft(null);
    setDeleteArmed(false);
    const next = new URLSearchParams(searchParams);
    if (g) {
      next.set("id", g.id);
      setFlyTarget({ key: Date.now(), points: geofenceBounds(g) });
    } else {
      next.delete("id");
    }
    setSearchParams(next, { replace: true });
  }

  function startCreate() {
    const tenant = ownTenantId ?? tenants[0]?.id ?? "";
    setError(null);
    setDeleteArmed(false);
    setDraft(emptyDraft(tenant, geofences.length));
    const next = new URLSearchParams(searchParams);
    next.delete("id");
    setSearchParams(next, { replace: true });
  }

  function startEdit(g: Geofence) {
    setError(null);
    setDeleteArmed(false);
    setDraft(draftFromGeofence(g));
    setFlyTarget({ key: Date.now(), points: geofenceBounds(g) });
  }

  async function save() {
    if (!draft || !geometryReady(draft)) return;
    setSaving(true);
    setError(null);
    const geometry =
      draft.shape === "circle"
        ? { shape: "circle" as const, center_lat: draft.center![0], center_lon: draft.center![1], radius_m: Math.round(draft.radius_m) }
        : { shape: "polygon" as const, polygon: draft.polygon };
    const settings = {
      name: draft.name.trim(),
      description: draft.description.trim() || null,
      color: draft.color,
      enabled: draft.enabled,
      notify_on_enter: draft.notify_on_enter,
      notify_on_exit: draft.notify_on_exit,
      dwell_minutes: draft.dwell_enabled ? draft.dwell_minutes : null,
      severity: draft.severity,
      hysteresis_m: draft.hysteresis_m,
      applies_to_all_devices: draft.applies_to_all_devices,
      device_ids: draft.applies_to_all_devices ? [] : draft.device_ids,
    };
    try {
      const saved = draft.id
        ? await api.updateGeofence(draft.id, { ...settings, ...geometry })
        : await api.createGeofence({ ...settings, ...geometry, tenant_id: draft.tenant_id || undefined } as GeofenceCreateBody);
      await reload();
      setDraft(null);
      const next = new URLSearchParams(searchParams);
      next.set("id", saved.id);
      setSearchParams(next, { replace: true });
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "error guardando la geocerca");
    } finally {
      setSaving(false);
    }
  }

  async function remove() {
    if (!draft?.id) return;
    if (!deleteArmed) {
      setDeleteArmed(true);
      return;
    }
    setSaving(true);
    try {
      await api.deleteGeofence(draft.id);
      await reload();
      select(null);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "error borrando la geocerca");
    } finally {
      setSaving(false);
      setDeleteArmed(false);
    }
  }

  async function toggleEnabled(g: Geofence) {
    try {
      await api.updateGeofence(g.id, { enabled: !g.enabled });
      await reload();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "error actualizando la geocerca");
    }
  }

  const editing = draft != null;
  const mapFences = editing ? geofences.filter((g) => g.id !== draft.id) : geofences;

  const map = (
    <div className={`h-[55vh] min-h-[320px] overflow-hidden rounded-lg border border-line lg:h-full ${editing ? "geofence-drawing" : ""}`}>
      <MapContainer center={MEXICO_CITY} zoom={5} minZoom={3} maxZoom={19} className="h-full w-full">
        <MapBaseLayer />
        <MapStyleSwitcher className="top-3 right-3" />
        {loaded && <FitOnce points={allFencePoints} fallback={positionPoints} />}
        <FlyToBounds target={flyTarget} />
        <GeofenceLayer
          geofences={mapFences}
          selectedId={editing ? null : selectedId}
          onSelect={editing ? undefined : (g) => select(g)}
          permanentLabels={mapFences.length <= 25}
        />
        {positions.map((p) => (
          <CircleMarker
            key={p.device_id}
            center={[p.lat, p.lon]}
            radius={5}
            interactive={!editing}
            pathOptions={{ color: "#ffffff", weight: 1.5, fillColor: "#037dfe", fillOpacity: 1 }}
          >
            {!editing && <Tooltip direction="top">{p.label}</Tooltip>}
          </CircleMarker>
        ))}
        {draft && <DraftShape draft={draft} onChange={setDraft} />}
        <DrawHandler draft={draft} onChange={setDraft} />
      </MapContainer>
    </div>
  );

  const editor = draft && (
    <Card className="space-y-4">
      <CardTitle
        action={
          <button onClick={() => setDraft(null)} className="text-xs text-ink-dim hover:text-ink">
            Cancelar
          </button>
        }
      >
        {draft.id ? "Editar geocerca" : "Nueva geocerca"}
      </CardTitle>
      {error && <Alert>{error}</Alert>}

      {isPlatform && !draft.id && (
        <Field label="Tenant">
          <Select value={draft.tenant_id} onChange={(e) => setDraft({ ...draft, tenant_id: e.target.value, device_ids: [] })}>
            <option value="" disabled>
              Selecciona...
            </option>
            {tenants.map((t) => (
              <option key={t.id} value={t.id}>
                {t.name}
              </option>
            ))}
          </Select>
        </Field>
      )}

      <Field label="Nombre">
        <Input value={draft.name} maxLength={120} placeholder="Ej. Bodega Norte" onChange={(e) => setDraft({ ...draft, name: e.target.value })} />
      </Field>

      <div className="space-y-2">
        <span className="text-xs font-medium text-ink-dim">Color</span>
        <div className="flex flex-wrap gap-2">
          {GEOFENCE_PALETTE.map((c) => (
            <button
              key={c}
              type="button"
              aria-label={`Color ${c}`}
              onClick={() => setDraft({ ...draft, color: c })}
              className={`h-8 w-8 rounded-full border-2 ${draft.color === c ? "border-white" : "border-transparent"}`}
              style={{ background: c }}
            />
          ))}
        </div>
      </div>

      <div className="space-y-2">
        <span className="text-xs font-medium text-ink-dim">Forma</span>
        <div className="grid grid-cols-2 gap-2">
          {(["circle", "polygon"] as const).map((shape) => (
            <button
              key={shape}
              type="button"
              onClick={() => setDraft({ ...draft, shape })}
              className={`rounded-sm border px-3 py-2 text-sm font-medium ${
                draft.shape === shape ? "border-brand-600 bg-brand-600/15 text-brand-500" : "border-line-strong bg-surface-2 text-ink-dim"
              }`}
            >
              {shape === "circle" ? "Círculo" : "Polígono"}
            </button>
          ))}
        </div>
        <p className="text-xs text-ink-dim">
          {draft.shape === "circle"
            ? draft.center
              ? "Toca el mapa para mover el centro, o arrastra el punto."
              : "Toca el mapa para fijar el centro."
            : draft.polygon.length < 3
              ? `Toca el mapa para agregar vértices (${draft.polygon.length}/3 mínimo).`
              : `${draft.polygon.length} vértices · ${formatArea(polygonAreaM2(draft.polygon))}. Arrastra un punto para ajustarlo.`}
        </p>
        {draft.shape === "circle" ? (
          <div className="space-y-1">
            <div className="flex items-center justify-between text-xs text-ink-dim">
              <span>Radio</span>
              <span className="font-medium text-ink">{Math.round(draft.radius_m)} m</span>
            </div>
            <input
              type="range"
              min={20}
              max={5000}
              step={10}
              value={Math.min(draft.radius_m, 5000)}
              onChange={(e) => setDraft({ ...draft, radius_m: Number(e.target.value) })}
              className="w-full accent-brand-600"
            />
            <Input
              type="number"
              min={10}
              max={100000}
              value={Math.round(draft.radius_m)}
              onChange={(e) => setDraft({ ...draft, radius_m: Number(e.target.value) })}
            />
          </div>
        ) : (
          <div className="flex gap-2">
            <Button variant="secondary" disabled={draft.polygon.length === 0} onClick={() => setDraft({ ...draft, polygon: draft.polygon.slice(0, -1) })}>
              Deshacer punto
            </Button>
            <Button variant="ghost" disabled={draft.polygon.length === 0} onClick={() => setDraft({ ...draft, polygon: [] })}>
              Limpiar
            </Button>
          </div>
        )}
      </div>

      <div className="space-y-2">
        <span className="text-xs font-medium text-ink-dim">Notificar cuando una unidad…</span>
        <Toggle checked={draft.notify_on_enter} onChange={(v) => setDraft({ ...draft, notify_on_enter: v })} label="Entra" />
        <Toggle checked={draft.notify_on_exit} onChange={(v) => setDraft({ ...draft, notify_on_exit: v })} label="Sale" />
        <Toggle
          checked={draft.dwell_enabled}
          onChange={(v) => setDraft({ ...draft, dwell_enabled: v })}
          label="Permanece adentro"
          hint="Una notificación por visita al cumplir el tiempo."
        />
        {draft.dwell_enabled && (
          <Field label="Minutos de permanencia">
            <Input
              type="number"
              min={1}
              max={10080}
              value={draft.dwell_minutes}
              onChange={(e) => setDraft({ ...draft, dwell_minutes: Number(e.target.value) })}
            />
          </Field>
        )}
        <p className="text-xs text-ink-dim">Todos los eventos quedan en el reporte aunque no notifiquen.</p>
      </div>

      <Field label="Importancia de la notificación">
        <Select value={draft.severity} onChange={(e) => setDraft({ ...draft, severity: e.target.value as AlarmSeverity })}>
          {(["info", "warning", "critical"] as const).map((s) => (
            <option key={s} value={s}>
              {SEVERITY_LABEL[s]}
            </option>
          ))}
        </Select>
      </Field>

      <div className="space-y-2">
        <span className="text-xs font-medium text-ink-dim">Aplica a</span>
        <div className="grid grid-cols-2 gap-2">
          {[true, false].map((all) => (
            <button
              key={String(all)}
              type="button"
              onClick={() => setDraft({ ...draft, applies_to_all_devices: all })}
              className={`rounded-sm border px-3 py-2 text-sm font-medium ${
                draft.applies_to_all_devices === all
                  ? "border-brand-600 bg-brand-600/15 text-brand-500"
                  : "border-line-strong bg-surface-2 text-ink-dim"
              }`}
            >
              {all ? "Todas las unidades" : "Unidades elegidas"}
            </button>
          ))}
        </div>
        {!draft.applies_to_all_devices && (
          <div className="space-y-2">
            <Input placeholder="Buscar unidad..." value={deviceFilter} onChange={(e) => setDeviceFilter(e.target.value)} />
            <div className="max-h-48 space-y-1 overflow-y-auto rounded-sm border border-line-strong p-2">
              {draftTenantDevices
                .filter((d) => d.label.toLowerCase().includes(deviceFilter.trim().toLowerCase()))
                .map((d) => (
                  <label key={d.id} className="flex cursor-pointer items-center gap-2 py-1 text-sm text-ink">
                    <input
                      type="checkbox"
                      className="h-4 w-4 accent-brand-600"
                      checked={draft.device_ids.includes(d.id)}
                      onChange={(e) =>
                        setDraft({
                          ...draft,
                          device_ids: e.target.checked ? [...draft.device_ids, d.id] : draft.device_ids.filter((x) => x !== d.id),
                        })
                      }
                    />
                    {d.label}
                  </label>
                ))}
              {draftTenantDevices.length === 0 && <p className="text-xs text-ink-dim">Este tenant no tiene unidades.</p>}
            </div>
            <p className="text-xs text-ink-dim">{draft.device_ids.length} seleccionadas</p>
          </div>
        )}
      </div>

      <details className="rounded-sm border border-line-strong bg-surface-2 px-3 py-2">
        <summary className="cursor-pointer text-sm font-medium text-ink">Opciones avanzadas</summary>
        <div className="mt-3 space-y-3">
          <Field label="Tolerancia de GPS al salir (m)">
            <Input
              type="number"
              min={0}
              max={500}
              value={draft.hysteresis_m}
              onChange={(e) => setDraft({ ...draft, hysteresis_m: Number(e.target.value) })}
            />
          </Field>
          <p className="text-xs text-ink-dim">Evita avisos repetidos cuando una unidad se estaciona justo en el borde.</p>
          <Field label="Descripción">
            <Input value={draft.description} maxLength={1000} onChange={(e) => setDraft({ ...draft, description: e.target.value })} />
          </Field>
          <Toggle checked={draft.enabled} onChange={(v) => setDraft({ ...draft, enabled: v })} label="Geocerca activa" />
        </div>
      </details>

      <div className="flex flex-wrap gap-2">
        <Button
          className="flex-1"
          disabled={saving || !draft.name.trim() || !geometryReady(draft) || (isPlatform && !draft.tenant_id)}
          onClick={save}
        >
          {saving ? "Guardando..." : "Guardar"}
        </Button>
        {draft.id && (
          <Button variant={deleteArmed ? "primary" : "secondary"} className={deleteArmed ? "bg-red-600 hover:bg-red-700" : ""} disabled={saving} onClick={remove}>
            {deleteArmed ? "¿Borrar? Confirmar" : "Borrar"}
          </Button>
        )}
      </div>
    </Card>
  );

  const detail = selected && !editing && (
    <Card className="space-y-4">
      <CardTitle
        action={
          <button onClick={() => select(null)} className="text-xs text-ink-dim hover:text-ink">
            Cerrar
          </button>
        }
      >
        <span className="flex items-center gap-2">
          <span className="h-3 w-3 shrink-0 rounded-full" style={{ background: selected.color }} />
          {selected.name}
        </span>
      </CardTitle>
      <div className="grid grid-cols-2 gap-3 text-sm">
        <div>
          <p className="text-xs text-ink-dim">Tamaño</p>
          <p className="font-medium text-ink">{geofenceSizeLabel(selected)}</p>
        </div>
        <div>
          <p className="text-xs text-ink-dim">Unidades adentro</p>
          <p className="font-medium text-ink">{selected.inside_count}</p>
        </div>
        <div>
          <p className="text-xs text-ink-dim">Notifica</p>
          <p className="font-medium text-ink">
            {[selected.notify_on_enter && "entrada", selected.notify_on_exit && "salida", selected.dwell_minutes && `${selected.dwell_minutes} min adentro`]
              .filter(Boolean)
              .join(" · ") || "no (solo reporte)"}
          </p>
        </div>
        <div>
          <p className="text-xs text-ink-dim">Aplica a</p>
          <p className="font-medium text-ink">
            {selected.applies_to_all_devices ? "Todas las unidades" : `${selected.device_ids.length} unidades`}
          </p>
        </div>
      </div>
      {selected.description && <p className="text-sm text-ink-dim">{selected.description}</p>}

      <div className="space-y-2">
        <p className="text-xs font-semibold tracking-wide text-ink-dim uppercase">Adentro ahora</p>
        {occupants.length === 0 ? (
          <p className="text-sm text-ink-dim">Ninguna unidad.</p>
        ) : (
          <ul className="divide-y divide-line">
            {occupants.map((o) => (
              <li key={o.device_id} className="flex items-center justify-between gap-2 py-1.5 text-sm">
                <button className="truncate text-left text-ink hover:underline" onClick={() => navigate(`/map?device=${o.device_id}`)}>
                  {o.device_label}
                </button>
                <span className="shrink-0 text-xs text-ink-dim">
                  {o.entered_at
                    ? `${o.entry_estimated ? "≥ " : ""}${formatDuration((Date.now() - new Date(o.entered_at).getTime()) / 1000)}`
                    : "—"}
                </span>
              </li>
            ))}
          </ul>
        )}
      </div>

      <div className="space-y-2">
        <p className="text-xs font-semibold tracking-wide text-ink-dim uppercase">Últimas 24 horas</p>
        {recentEvents.length === 0 ? (
          <p className="text-sm text-ink-dim">Sin eventos.</p>
        ) : (
          <ul className="divide-y divide-line">
            {recentEvents.map((e) => (
              <li key={e.id} className="flex items-center justify-between gap-2 py-1.5 text-sm">
                <span className="flex min-w-0 items-center gap-2">
                  <Badge tone={EVENT_TONE[e.event_type]}>{GEOFENCE_EVENT_LABEL[e.event_type]}</Badge>
                  <span className="truncate text-ink">{e.device_label}</span>
                </span>
                <span className="shrink-0 text-xs text-ink-dim">
                  {new Date(e.time).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}
                  {e.duration_s != null && ` · ${formatDuration(e.duration_s)}`}
                </span>
              </li>
            ))}
          </ul>
        )}
      </div>

      {canManage && (
        <div className="flex flex-wrap gap-2">
          <Button className="flex-1" onClick={() => startEdit(selected)}>
            Editar
          </Button>
          <Button variant="secondary" onClick={() => toggleEnabled(selected)}>
            {selected.enabled ? "Pausar" : "Activar"}
          </Button>
        </div>
      )}
    </Card>
  );

  const list = (
    <Card className="space-y-3">
      <CardTitle>{`Geocercas (${geofences.length})`}</CardTitle>
      {error && !editing && <Alert>{error}</Alert>}
      <Input placeholder="Buscar geocerca..." value={search} onChange={(e) => setSearch(e.target.value)} />
      {!loaded ? (
        <p className="py-6 text-center text-sm text-ink-dim">Cargando...</p>
      ) : filtered.length === 0 ? (
        <EmptyState>
          {geofences.length === 0
            ? canManage
              ? "Todavía no hay geocercas. Crea la primera con “Nueva geocerca”."
              : "Todavía no hay geocercas en tu flota."
            : "Ninguna coincide con la búsqueda."}
        </EmptyState>
      ) : (
        <ul className="divide-y divide-line">
          {filtered.map((g) => (
            <li key={g.id}>
              <button
                onClick={() => select(g)}
                className={`flex w-full items-center gap-3 px-1 py-2.5 text-left hover:bg-surface-2 ${g.id === selectedId ? "bg-brand-600/10" : ""}`}
              >
                <span
                  className="h-9 w-9 shrink-0 rounded-md border-2"
                  style={{ borderColor: g.color, background: `${g.color}33`, borderRadius: g.shape === "circle" ? 9999 : 6 }}
                />
                <span className="min-w-0 flex-1">
                  <span className="block truncate text-sm font-medium text-ink">{g.name}</span>
                  <span className="block truncate text-xs text-ink-dim">
                    {geofenceSizeLabel(g)}
                    {isPlatform && tenantName.get(g.tenant_id) ? ` · ${tenantName.get(g.tenant_id)}` : ""}
                  </span>
                </span>
                <span className="flex shrink-0 flex-col items-end gap-1">
                  {g.enabled ? <Badge tone={SEVERITY_TONE[g.severity]}>{g.inside_count} adentro</Badge> : <Badge tone="muted">pausada</Badge>}
                </span>
              </button>
            </li>
          ))}
        </ul>
      )}
    </Card>
  );

  return (
    <PageContainer wide>
      <PageHeader
        title="Geocercas"
        description="Zonas en el mapa que generan avisos cuando una unidad entra, sale o permanece."
        action={
          canManage && !editing ? (
            <Button onClick={startCreate} disabled={isPlatform && tenants.length === 0}>
              + Nueva geocerca
            </Button>
          ) : undefined
        }
      />
      <div className="flex flex-col gap-4 lg:grid lg:h-[calc(100vh-10rem)] lg:grid-cols-[380px_minmax(0,1fr)]">
        {/*
         * On mobile the map comes first (on top) -- order- flips the visual
         * order on large screens without duplicating markup.
         */}
        <div className="order-2 space-y-4 lg:order-1 lg:overflow-y-auto lg:pr-1">
          {editor ?? detail}
          {!editing && list}
        </div>
        <div className="order-1 lg:order-2 lg:h-full">{map}</div>
      </div>
    </PageContainer>
  );
}
