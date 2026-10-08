import { useEffect, useState } from "react";
import { api, type Geofence } from "./api";

// Geofence layer for maps that are NOT the Geofences page (live Map, Route
// history): a single request on mount (geofences rarely change -- never polled),
// and the show/hide preference remembered per browser. localStorage only for
// this per-viewer convenience, wrapped in try/catch (private mode, etc.).
const STORAGE_KEY = "omd-show-geofences";

function readPref(): boolean {
  try {
    return localStorage.getItem(STORAGE_KEY) !== "0";
  } catch {
    return true;
  }
}

export function useGeofenceOverlay() {
  const [geofences, setGeofences] = useState<Geofence[]>([]);
  const [visible, setVisible] = useState(readPref);

  useEffect(() => {
    let cancelled = false;
    api
      .listGeofences({ limit: 500 })
      .then((page) => {
        if (!cancelled) setGeofences(page.items);
      })
      // Missing geofences are not an error worth showing on a map whose main
      // purpose is something else.
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, []);

  function toggle() {
    setVisible((v) => {
      try {
        localStorage.setItem(STORAGE_KEY, v ? "0" : "1");
      } catch {
        // preference not persisted, no harm
      }
      return !v;
    });
  }

  return { geofences, visible, toggle };
}
