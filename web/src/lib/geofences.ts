import type { AlarmSeverity, Geofence, GeofenceEventType, LatLonTuple } from "./api";

// Shared utilities for the geofence module -- a single place for labels, palette
// and formatting, used by the Geofences page, the map layer (GeofenceLayer) and
// the report (Reports.tsx).

export const GEOFENCE_EVENT_LABEL: Record<GeofenceEventType, string> = {
  enter: "Entrada",
  exit: "Salida",
  dwell: "Permanencia",
};

export const SEVERITY_LABEL: Record<AlarmSeverity, string> = {
  info: "Informativa",
  warning: "Advertencia",
  critical: "Crítica",
};

// Suggested geofence color palette -- distinguishable from each other and on
// dark/light maps; the user can pick any other hex color.
export const GEOFENCE_PALETTE = ["#037dfe", "#10b981", "#f59e0b", "#ef4444", "#a855f7", "#ec4899", "#14b8a6", "#64748b"];

export function formatDuration(seconds: number | null | undefined): string {
  if (seconds == null) return "—";
  if (seconds < 60) return `${Math.round(seconds)} s`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes} min`;
  const hours = Math.floor(minutes / 60);
  const rem = minutes % 60;
  if (hours < 24) return rem ? `${hours} h ${rem} min` : `${hours} h`;
  const days = Math.floor(hours / 24);
  return `${days} d ${hours % 24} h`;
}

export function formatDistance(meters: number): string {
  return meters >= 1000 ? `${(meters / 1000).toFixed(meters >= 10000 ? 0 : 1)} km` : `${Math.round(meters)} m`;
}

// Map bounds to fit one or more geofences.
export function geofenceBounds(g: Pick<Geofence, "shape" | "center_lat" | "center_lon" | "radius_m" | "polygon">): LatLonTuple[] {
  if (g.shape === "polygon" && g.polygon) return g.polygon;
  if (g.center_lat == null || g.center_lon == null || g.radius_m == null) return [];
  const dLat = g.radius_m / 110574;
  const dLon = g.radius_m / (111320 * Math.max(Math.cos((g.center_lat * Math.PI) / 180), 0.01));
  return [
    [g.center_lat - dLat, g.center_lon - dLon],
    [g.center_lat + dLat, g.center_lon + dLon],
  ];
}

// Approximate area (m²) of a polygon -- local equirectangular projection,
// informational only in the editor.
export function polygonAreaM2(poly: LatLonTuple[]): number {
  if (poly.length < 3) return 0;
  const lat0 = (poly.reduce((acc, p) => acc + p[0], 0) / poly.length) * (Math.PI / 180);
  const kx = 111320 * Math.cos(lat0);
  const ky = 110574;
  let sum = 0;
  for (let i = 0; i < poly.length; i++) {
    const [lat1, lon1] = poly[i];
    const [lat2, lon2] = poly[(i + 1) % poly.length];
    sum += lon1 * kx * (lat2 * ky) - lon2 * kx * (lat1 * ky);
  }
  return Math.abs(sum) / 2;
}

export function formatArea(m2: number): string {
  if (m2 >= 1_000_000) return `${(m2 / 1_000_000).toFixed(2)} km²`;
  if (m2 >= 10_000) return `${(m2 / 10_000).toFixed(1)} ha`;
  return `${Math.round(m2)} m²`;
}

export function geofenceSizeLabel(g: Pick<Geofence, "shape" | "radius_m" | "polygon">): string {
  if (g.shape === "circle" && g.radius_m != null) return `Radio ${formatDistance(g.radius_m)}`;
  if (g.polygon) return `${g.polygon.length} vértices · ${formatArea(polygonAreaM2(g.polygon))}`;
  return "";
}
