import { useEffect, useRef, useState } from "react";
import L from "leaflet";
import { TileLayer, useMap } from "react-leaflet";
import { useMapProvider, type MapProviderConfig } from "../lib/mapProviders";
import { LayersIcon } from "./icons";
import { MAP_STYLES, OPENFREEMAP_ATTRIBUTION, useMapStyle, type MapStyleDef } from "../lib/mapStyle";

// SINGLE base layer for every map in the app (live Map, History, Geofences).
// Decision order:
// 1. super_admin forced a raster provider (platform_map_settings) -> always that
//   (operational escape hatch).
// 2. the style chosen by the user (lib/mapStyle.ts, OpenFreeMap "Streets" by
//   default).
// 3. if the vector style fails (no WebGL, OpenFreeMap down, repeated tile
//   errors) -> automatic raster failover (OSM -> Esri), with no user action.
//
// MapLibre GL (~800 KB) is loaded with dynamic import() ONLY when a vector map
// mounts: the rest of the app does not pay that weight.

const VECTOR_ERROR_THRESHOLD = 6;
const VECTOR_LOAD_TIMEOUT_MS = 12_000;

function VectorBaseLayer({ styleUrl, onFail }: { styleUrl: string; onFail: () => void }) {
  const map = useMap();
  const onFailRef = useRef(onFail);
  onFailRef.current = onFail;

  useEffect(() => {
    let cancelled = false;
    let layer: L.Layer | null = null;
    let errors = 0;
    let loadTimer: ReturnType<typeof setTimeout> | null = null;
    (async () => {
      await import("maplibre-gl/dist/maplibre-gl.css");
      // Vite bundles the MapLibre worker separately (its default URL, relative
      // to import.meta.url, does not exist after bundling -- "Worker failed to
      // load").
      const [maplibre, workerModule] = await Promise.all([
        import("maplibre-gl"),
        import("maplibre-gl/dist/maplibre-gl-worker.mjs?worker&url"),
      ]);
      maplibre.setWorkerUrl(workerModule.default);
      await import("@maplibre/maplibre-gl-leaflet");
      if (cancelled) return;
      // The plugin registers L.maplibreGL on the SAME Leaflet instance
      // react-leaflet uses (imported as a module, not as a global).
      const factory = (L as unknown as { maplibreGL: (o: object) => L.Layer & { getMaplibreMap: () => { on: (e: string, cb: () => void) => void; isStyleLoaded: () => boolean | void } } }).maplibreGL;
      const glLayer = factory({ style: styleUrl, attribution: OPENFREEMAP_ATTRIBUTION });
      glLayer.addTo(map);
      layer = glLayer;
      const gl = glLayer.getMaplibreMap();
      // Safety net: an ASYNC failure (worker that does not load, unreachable
      // style) may not emit enough "error" events -- without this deadline the
      // map could stay blank forever instead of switching to raster.
      let ready = false;
      const markReady = () => {
        ready = true;
        if (loadTimer) clearTimeout(loadTimer);
      };
      loadTimer = setTimeout(() => {
        // `load` does not always arrive (the map can already be visible while
        // the deadline expires) -- check the real state before abandoning the
        // vector style.
        if (ready || gl.isStyleLoaded()) return;
        console.warn("[map] vector style did not finish loading in time, switching to raster tiles");
        onFailRef.current();
      }, VECTOR_LOAD_TIMEOUT_MS);
      gl.on("load", markReady);
      gl.on("styledata", markReady);
      gl.on("error", () => {
        errors += 1;
        if (errors === VECTOR_ERROR_THRESHOLD) {
          console.warn("[map] repeated vector style errors, switching to raster tiles");
          onFailRef.current();
        }
      });
    })().catch((err) => {
      console.warn("[map] could not start the vector map (no WebGL?), using raster", err);
      if (!cancelled) onFailRef.current();
    });
    return () => {
      cancelled = true;
      if (loadTimer) clearTimeout(loadTimer);
      if (layer) map.removeLayer(layer);
    };
  }, [map, styleUrl]);

  return null;
}

function RasterBase({ cfg, handlers }: { cfg: MapProviderConfig; handlers?: { tileerror: () => void; tileload: () => void } }) {
  return (
    <TileLayer
      key={cfg.id}
      attribution={cfg.attribution}
      url={cfg.url}
      subdomains={cfg.subdomains}
      maxZoom={cfg.maxZoom}
      eventHandlers={handlers}
    />
  );
}

function RasterStyle({ style }: { style: Extract<MapStyleDef, { kind: "raster" }> }) {
  return (
    <>
      <TileLayer key={style.id} attribution={style.attribution} url={style.url} subdomains={style.subdomains} maxZoom={style.maxZoom} />
      {style.overlayUrl && <TileLayer key={`${style.id}-labels`} url={style.overlayUrl} maxZoom={style.maxZoom} />}
    </>
  );
}

export function MapBaseLayer() {
  const { provider, isForced, tileEventHandlers } = useMapProvider();
  const [style] = useMapStyle();
  const [failedStyle, setFailedStyle] = useState<string | null>(null);

  if (isForced) return <RasterBase cfg={provider} handlers={tileEventHandlers} />;
  if (style.kind === "raster") return <RasterStyle style={style} />;
  if (failedStyle === style.id) return <RasterBase cfg={provider} handlers={tileEventHandlers} />;
  return <VectorBaseLayer key={style.id} styleUrl={style.styleUrl} onFail={() => setFailedStyle(style.id)} />;
}


// Floating style picker INSIDE the map (position absolute relative to the map
// container, never position:fixed). disableClickPropagation: a tap on the picker
// never reaches the map (on Geofences, a map tap adds a vertex).
export function MapStyleSwitcher({ className = "top-3 right-3" }: { className?: string }) {
  const [style, setStyle] = useMapStyle();
  const { isForced } = useMapProvider();
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!ref.current) return;
    L.DomEvent.disableClickPropagation(ref.current);
    L.DomEvent.disableScrollPropagation(ref.current);
  }, []);

  return (
    <div ref={ref} className={`absolute z-[1000] ${className}`}>
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        aria-expanded={open}
        aria-label="Estilo del mapa"
        title="Estilo del mapa"
        className="glass flex h-9 w-9 items-center justify-center rounded-xl text-ink shadow-lg hover:text-brand-400"
      >
        <LayersIcon />
      </button>
      {open && (
        <div className="glass-strong absolute right-0 mt-2 w-60 rounded-2xl p-2">
          <p className="px-1.5 pb-1.5 text-[11px] font-semibold tracking-wide text-ink-dim uppercase">Estilo del mapa</p>
          {isForced && (
            <p className="mb-2 rounded-lg bg-accent-warn/15 px-2 py-1.5 text-[11px] text-ink">
              Plataforma fijó un proveedor temporalmente; tu elección se aplicará al retirarlo.
            </p>
          )}
          <div className="grid grid-cols-3 gap-1.5">
            {MAP_STYLES.map((s) => (
              <button
                key={s.id}
                type="button"
                onClick={() => {
                  setStyle(s.id);
                  setOpen(false);
                }}
                className={`flex flex-col items-center gap-1 rounded-xl p-1.5 text-[11px] transition-colors ${
                  s.id === style.id ? "bg-brand-600/20 text-brand-400" : "text-ink-dim hover:bg-fg/5 hover:text-ink"
                }`}
              >
                <span
                  className={`h-10 w-full rounded-lg border ${s.id === style.id ? "border-brand-500" : "border-fg/10"}`}
                  style={{ background: s.swatch }}
                />
                {s.label}
              </button>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
