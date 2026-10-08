import { useEffect, useState } from "react";
import { api, type Alarm } from "./api";

// Simple polling instead of WebSockets on purpose -- nothing here needs to
// update in under 15s. If lower latency is ever needed (critical alarm ->
// immediate notification), reevaluate then, not before.
const POLL_MS = 15_000;

export function useUnacknowledgedAlarms() {
  const [alarms, setAlarms] = useState<Alarm[]>([]);

  useEffect(() => {
    let cancelled = false;
    async function poll() {
      try {
        // Warning/critical only: an informational event (ignition, geofence
        // entry) does not turn a unit into "has alarm" on the map -- with active
        // geofences that would paint the whole fleet red.
        const data = await api.listAlarms({ unacknowledgedOnly: true, minSeverity: "warning", limit: 500 });
        if (!cancelled) setAlarms(data);
      } catch {
        // Silent on purpose: this runs in the background on every page and a
        // transient network error must not interrupt anything -- the Alarms page
        // does show explicit errors when the user opens it.
      }
    }
    poll();
    const interval = setInterval(poll, POLL_MS);
    return () => {
      cancelled = true;
      clearInterval(interval);
    };
  }, []);

  return alarms;
}
