import { useEffect, useRef } from "react";
import L from "leaflet";
import attachHotline from "leaflet-hotline";
import { useMap } from "react-leaflet";

// leaflet-hotline (github.com/iosphere/Leaflet.hotline, BSD-2-Clause, audited
// before installing -- no network/eval/exec calls in its source) draws a
// speed-colored line in the route history module. It renders the gradient on
// <canvas>, efficient even with thousands of points (unlike splitting the route
// into dozens of native <Polyline>s, one element per segment). The package
// exports the plugin FACTORY (not applied) -- it is called ONCE at module level
// to register L.hotline()/L.Hotline on the SAME Leaflet instance the rest of the
// app uses.
attachHotline(L);

// Same tones as SpeedGauge.tsx (green/amber/red), but on a FIXED scale (0-120
// km/h) instead of relative to a vehicle's configured limit: this view can show
// several units without a configured limit at once, and a per-dataset relative
// scale would make a slow trip look "all red" against itself, losing the real
// comparison between units/days.
export const HOTLINE_MIN_KMH = 0;
export const HOTLINE_MAX_KMH = 120;
export const HOTLINE_PALETTE: Record<number, string> = { 0.0: "#10b981", 0.5: "#daa520", 1.0: "#ef4444" };

export interface HotlinePoint {
  lat: number;
  lon: number;
  speedKmh: number;
}

// Recreates the whole layer instead of mutating it in place -- this runs only
// when the history is reloaded (explicit "Load" click), never on every
// render/tick, so the rebuild cost is irrelevant compared to guaranteeing
// min/max/palette never drift from the current points.
export function HotlinePolyline({ points, weight = 4 }: { points: HotlinePoint[]; weight?: number }) {
  const map = useMap();
  const layerRef = useRef<L.Hotline | null>(null);

  useEffect(() => {
    layerRef.current?.remove();
    layerRef.current = null;
    if (points.length < 2) return;

    const latlngs: L.HotlineLatLng[] = points.map((p) => [p.lat, p.lon, p.speedKmh]);
    layerRef.current = L.hotline(latlngs, {
      min: HOTLINE_MIN_KMH,
      max: HOTLINE_MAX_KMH,
      palette: HOTLINE_PALETTE,
      weight,
      outlineWidth: 1,
      outlineColor: "#00000080",
    }).addTo(map);

    return () => {
      layerRef.current?.remove();
      layerRef.current = null;
    };
  }, [points, weight, map]);

  return null;
}
