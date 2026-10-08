import { useEffect, useRef, useState } from "react";
import { api, type MapProvider } from "./api";

// Map resilience: relying only on OpenStreetMap's tile servers has no fallback
// if they fail or start rate-limiting (their usage policy explicitly discourages
// heavy commercial traffic). Hence automatic failover between several providers,
// plus a super_admin manual override (see
// api/app/routers/platform.py::update_map_settings) for when one fails without
// the system detecting it -- an escape hatch, never the expected path.
export interface MapProviderConfig {
  id: Exclude<MapProvider, "auto">;
  label: string;
  url: string;
  attribution: string;
  // ALWAYS a string, never undefined: Leaflet's TileLayer._getSubdomain() reads
  // this.options.subdomains.length without any check, so passing
  // subdomains={undefined} from React (Esri does not use {s} in its URL)
  // overrode Leaflet's internal default ('abc') with undefined and crashed the
  // WHOLE page with an uncaught exception when the first tile mounted.
  subdomains: string;
  maxZoom: number;
}

// CARTO no longer serves anonymous raster tiles -- they require a free API key
// (see docs.carto.com/faqs/carto-basemaps). The key is public in the frontend
// bundle on purpose (like any domain-restricted map key, never a real server
// secret) -- if CARTO offers domain restriction in its dashboard, configuring it
// there is the real defense against quota abuse, not hiding the key (impossible
// in a Vite bundle anyway).
const CARTO_API_KEY = import.meta.env.VITE_CARTO_API_KEY as string | undefined;

export const MAP_PROVIDERS: Record<Exclude<MapProvider, "auto">, MapProviderConfig> = {
  osm: {
    id: "osm",
    label: "OpenStreetMap",
    url: "https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png",
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
    subdomains: "abc",
    maxZoom: 19,
  },
  // No API key, free -- the simplest real fallback: if OSM fails, this needs no
  // extra configuration.
  esri: {
    id: "esri",
    label: "Esri World Street Map",
    url: "https://server.arcgisonline.com/ArcGIS/rest/services/World_Street_Map/MapServer/tile/{z}/{y}/{x}",
    attribution: "Tiles &copy; Esri -- Source: Esri, HERE, Garmin, USGS, NGA, EPA, USDA",
    subdomains: "", // its URL does not use {s} -- empty string, never undefined
    maxZoom: 19,
  },
  carto: {
    id: "carto",
    label: "CARTO Voyager",
    url: `https://{s}.basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}.png${
      CARTO_API_KEY ? `?api_key=${CARTO_API_KEY}` : ""
    }`,
    attribution: '&copy; <a href="https://carto.com/attributions">CARTO</a> &copy; OpenStreetMap contributors',
    subdomains: "abcd",
    maxZoom: 20,
  },
};

// Automatic failover order -- OSM first, Esri second (no API key, immediate
// fallback), CARTO last and ONLY if a key is configured (without a key its tiles
// come out stamped "API KEY REQUIRED" -- worse than not offering it at all).
export const AUTO_FAILOVER_ORDER: Array<Exclude<MapProvider, "auto">> = CARTO_API_KEY
  ? ["osm", "esri", "carto"]
  : ["osm", "esri"];

const SESSION_STORAGE_KEY = "openmdvr_map_provider_index";
// Sample window (not time window) -- a map loads many tiles at once on pan/zoom,
// so a simple count of the last N results is more stable than counting
// consecutive errors (one lost tile among 20 successful ones must not take down
// the whole provider).
const WINDOW_SIZE = 20;
const MIN_SAMPLES_BEFORE_FAILOVER = 8;
const FAILURE_RATIO_THRESHOLD = 0.5;

function readSavedIndex(): number {
  try {
    const saved = sessionStorage.getItem(SESSION_STORAGE_KEY);
    const idx = saved ? parseInt(saved, 10) : 0;
    return Number.isFinite(idx) && idx >= 0 && idx < AUTO_FAILOVER_ORDER.length ? idx : 0;
  } catch {
    // sessionStorage may be unavailable (very restrictive private browsing) --
    // degrades to "always start with the first one", must never break the map.
    return 0;
  }
}

// useMapProvider decides WHICH tile provider to use right now: 1. If a
// super_admin forced a specific one (platform_map_settings.active_provider !=
// 'auto'), THAT one is always used -- no automatic failover, it is an explicit
// manual override. 2. If 'auto' (the expected state in normal operation), walk
// AUTO_FAILOVER_ORDER and advance to the next provider when the current one's
// tile error rate exceeds the threshold -- never going back within the same tab
// (avoids oscillating if the primary half-recovers); a new tab starts again with
// OSM.
export function useMapProvider(): {
  provider: MapProviderConfig;
  isForced: boolean;
  tileEventHandlers: { tileerror: () => void; tileload: () => void };
} {
  const [forced, setForced] = useState<MapProvider | null>(null); // null = still loading
  const [autoIndex, setAutoIndex] = useState<number>(readSavedIndex);
  const samplesRef = useRef<number[]>([]); // 1 = error, 0 = success

  // isForced/autoIndex are read from refs inside recordSample (not directly from
  // this render's closure) -- react-leaflet may not rebind eventHandlers on
  // every render, so a closure captured at initial mount could keep reading a
  // stale autoIndex forever and never advance (or, worse, allow an out-of-range
  // index). Refs always reflect the latest render without depending on that
  // rebinding.
  const isForcedRef = useRef(false);
  const autoIndexRef = useRef(autoIndex);
  autoIndexRef.current = autoIndex;

  useEffect(() => {
    let cancelled = false;
    api
      .getMapSettings()
      .then((s) => {
        if (!cancelled) setForced(s.active_provider);
      })
      .catch(() => {
        // Fail open to 'auto' -- an error fetching the setting must NEVER block
        // the map; automatic failover is a reasonable safety net on its own.
        if (!cancelled) setForced("auto");
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const isForced = forced !== null && forced !== "auto";
  isForcedRef.current = isForced;
  const provider = isForced
    ? MAP_PROVIDERS[forced as Exclude<MapProvider, "auto">]
    : MAP_PROVIDERS[AUTO_FAILOVER_ORDER[autoIndex]];

  function recordSample(isError: boolean) {
    if (isForcedRef.current) return; // manual override: never switches on its own
    const currentIndex = autoIndexRef.current;
    if (currentIndex >= AUTO_FAILOVER_ORDER.length - 1) return; // already the last one, nowhere to advance

    const samples = samplesRef.current;
    samples.push(isError ? 1 : 0);
    if (samples.length > WINDOW_SIZE) samples.shift();
    if (samples.length < MIN_SAMPLES_BEFORE_FAILOVER) return;

    const errorRatio = samples.reduce((sum, s) => sum + s, 0) / samples.length;
    if (errorRatio >= FAILURE_RATIO_THRESHOLD) {
      const from = AUTO_FAILOVER_ORDER[currentIndex];
      const to = AUTO_FAILOVER_ORDER[currentIndex + 1];
      console.warn(`[map] tile provider "${from}" has ${Math.round(errorRatio * 100)}% recent errors, switching to "${to}"`);
      samplesRef.current = [];
      autoIndexRef.current = currentIndex + 1;
      try {
        sessionStorage.setItem(SESSION_STORAGE_KEY, String(currentIndex + 1));
      } catch {
        // Same rule as readSavedIndex: sessionStorage is a convenience, never a
        // hard dependency.
      }
      setAutoIndex(currentIndex + 1);
    }
  }

  return {
    provider,
    isForced,
    tileEventHandlers: {
      tileerror: () => recordSample(true),
      tileload: () => recordSample(false),
    },
  };
}
