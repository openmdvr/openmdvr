import { useSyncExternalStore } from "react";

// Base map styles the END USER can choose. Vector styles come from OpenFreeMap
// (tiles.openfreemap.org: free, no API key, commercial use allowed; requires the
// attribution below) and are drawn with MapLibre GL inside the same Leaflet map
// (see components/MapBaseLayer.tsx) -- markers, clusters, geofences and trails
// are still regular react-leaflet layers on top.
//
// The preference is per viewer (localStorage, a per-browser convenience wrapped
// in try/catch) and is shared across ALL maps open at once via
// useSyncExternalStore: changing the style on the Map also changes History or
// Geofences without reloading.

export const OPENFREEMAP_ATTRIBUTION =
  '<a href="https://openfreemap.org" target="_blank" rel="noopener">OpenFreeMap</a> © <a href="https://www.openmaptiles.org/" target="_blank" rel="noopener">OpenMapTiles</a> Data from <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener">OpenStreetMap</a>';

export type MapStyleId = "streets" | "light" | "dark" | "bright" | "satellite" | "osm";

export type MapStyleDef =
  | { id: MapStyleId; label: string; kind: "vector"; styleUrl: string; swatch: string }
  | {
      id: MapStyleId;
      label: string;
      kind: "raster";
      url: string;
      subdomains: string;
      attribution: string;
      maxZoom: number;
      swatch: string;
      // Optional raster overlay (street/place labels over the satellite
      // imagery).
      overlayUrl?: string;
    };

export const MAP_STYLES: MapStyleDef[] = [
  { id: "streets", label: "Calles", kind: "vector", styleUrl: "https://tiles.openfreemap.org/styles/liberty", swatch: "linear-gradient(135deg,#f2efe9 0 55%,#a8d4f0 55% 70%,#f7c873 70%)" },
  { id: "light", label: "Claro", kind: "vector", styleUrl: "https://tiles.openfreemap.org/styles/positron", swatch: "linear-gradient(135deg,#fafafa 0 60%,#d4dadc 60%)" },
  { id: "dark", label: "Oscuro", kind: "vector", styleUrl: "https://tiles.openfreemap.org/styles/dark", swatch: "linear-gradient(135deg,#1f2328 0 60%,#3b4250 60%)" },
  { id: "bright", label: "Brillante", kind: "vector", styleUrl: "https://tiles.openfreemap.org/styles/bright", swatch: "linear-gradient(135deg,#f8f4f0 0 50%,#9ed27a 50% 70%,#e67e5c 70%)" },
  {
    id: "satellite",
    label: "Satélite",
    kind: "raster",
    url: "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
    overlayUrl: "https://server.arcgisonline.com/ArcGIS/rest/services/Reference/World_Boundaries_and_Places/MapServer/tile/{z}/{y}/{x}",
    subdomains: "",
    attribution: "Imagery &copy; Esri, Maxar, Earthstar Geographics",
    maxZoom: 19,
    swatch: "linear-gradient(135deg,#2f4a2a 0 40%,#5b6b3f 40% 70%,#8a7d5a 70%)",
  },
  {
    id: "osm",
    label: "OSM clásico",
    kind: "raster",
    url: "https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png",
    subdomains: "abc",
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
    maxZoom: 19,
    swatch: "linear-gradient(135deg,#f2efe9 0 55%,#aad3df 55% 70%,#fcd6a4 70%)",
  },
];

export const DEFAULT_MAP_STYLE: MapStyleId = "streets";
const STORAGE_KEY = "omd-map-style";
const listeners = new Set<() => void>();

function read(): MapStyleId {
  try {
    const v = localStorage.getItem(STORAGE_KEY) as MapStyleId | null;
    return v && MAP_STYLES.some((s) => s.id === v) ? v : DEFAULT_MAP_STYLE;
  } catch {
    return DEFAULT_MAP_STYLE;
  }
}

let current: MapStyleId = read();

export function setMapStyle(id: MapStyleId) {
  current = id;
  try {
    localStorage.setItem(STORAGE_KEY, id);
  } catch {
    // preference not persisted (private mode); still works in memory
  }
  listeners.forEach((l) => l());
}

export function useMapStyle(): [MapStyleDef, (id: MapStyleId) => void] {
  const id = useSyncExternalStore(
    (cb) => {
      listeners.add(cb);
      return () => listeners.delete(cb);
    },
    () => current,
  );
  return [MAP_STYLES.find((s) => s.id === id) ?? MAP_STYLES[0], setMapStyle];
}
