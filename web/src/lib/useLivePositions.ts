import { useEffect, useRef, useState } from "react";
import { api, type DevicePosition } from "./api";

// Ignition/power state arriving through the SAME SSE stream as positions (see
// live_positions.py, migration 0047), so changes show without reloading.
// Distinguished from the position payload by the "type" field (see es.onmessage
// below), which positions never carry.
export interface DeviceStatusEvent {
  type: "device_status";
  tenant_id: string;
  device_id: string;
  ignition_on: boolean | null;
  power_connected: boolean | null;
  // Migration 0049 -- without this, the icon changed live but "X ago" kept
  // reading the stale value from the last REST poll (up to 60s behind) instead
  // of the change that just happened.
  ignition_changed_at: string | null;
  power_changed_at: string | null;
}

// Real-time GPS positions -- replaces periodic polling with a real stream (SSE,
// see api/app/routers/positions.py). Design:
//
// - NEVER rely on EventSource's native retry: the one-time ticket
//   (api.createPositionStreamTicket) breaks it -- an automatic browser retry
//   would reuse the already-consumed ticket and always get 401. This hook
//   implements its own reconnection loop (close → request a new ticket → open a
//   new stream) with exponential backoff.
// - Low-frequency REST reconciliation in parallel with the push (not only on
//   reconnect): covers the most dangerous class of push bug -- the client THINKS
//   it is connected but the server silently stopped forwarding events.
// - Explicit close of the active EventSource on logout
//   (closeActivePositionStream, called from auth.tsx) -- otherwise the socket
//   could survive logout and a subsequent login in the same tab could keep
//   receiving the previous tenant's data.

const RECONNECT_BACKOFF_INITIAL_MS = 1_000;
const RECONNECT_BACKOFF_MAX_MS = 30_000;
const RECONCILIATION_POLL_MS = 90_000;

export type LiveConnectionState = "connecting" | "open" | "reconnecting";

let activeStreamCloser: (() => void) | null = null;

// Called from auth.tsx on logout -- see the docstring above.
export function closeActivePositionStream() {
  activeStreamCloser?.();
}

function mergeNewer(target: Map<string, DevicePosition>, incoming: DevicePosition[]): Map<string, DevicePosition> {
  const next = new Map(target);
  for (const pos of incoming) {
    const current = next.get(pos.device_id);
    if (!current || new Date(pos.time).getTime() >= new Date(current.time).getTime()) {
      next.set(pos.device_id, pos);
    }
  }
  return next;
}

export function useLivePositions() {
  const [positionByDevice, setPositionByDevice] = useState<Map<string, DevicePosition>>(new Map());
  const [deviceStatusByDevice, setDeviceStatusByDevice] = useState<Map<string, DeviceStatusEvent>>(new Map());
  const [connectionState, setConnectionState] = useState<LiveConnectionState>("connecting");
  const stoppedRef = useRef(false);
  const esRef = useRef<EventSource | null>(null);
  const reconnectTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const backoffRef = useRef(RECONNECT_BACKOFF_INITIAL_MS);

  useEffect(() => {
    stoppedRef.current = false;

    function scheduleReconnect() {
      if (stoppedRef.current) return;
      setConnectionState("reconnecting");
      reconnectTimeoutRef.current = setTimeout(() => {
        backoffRef.current = Math.min(backoffRef.current * 2, RECONNECT_BACKOFF_MAX_MS);
        openStream();
      }, backoffRef.current);
    }

    async function openStream() {
      if (stoppedRef.current) return;
      try {
        // Seed/resync via REST before (re)opening the stream, so the map does
        // not keep showing potentially stale positions while the push is
        // re-established.
        const seed = await api.latestPositions();
        if (stoppedRef.current) return;
        setPositionByDevice((prev) => mergeNewer(prev, seed));

        const { ticket } = await api.createPositionStreamTicket();
        if (stoppedRef.current) return;

        const es = new EventSource(api.positionStreamUrl(ticket));
        esRef.current = es;
        es.onopen = () => {
          backoffRef.current = RECONNECT_BACKOFF_INITIAL_MS;
          setConnectionState("open");
        };
        es.onmessage = (event) => {
          try {
            const data = JSON.parse(event.data);
            if (data && data.type === "device_status") {
              const status: DeviceStatusEvent = data;
              setDeviceStatusByDevice((prev) => {
                const next = new Map(prev);
                next.set(status.device_id, status);
                return next;
              });
              return;
            }
            const pos: DevicePosition = data;
            setPositionByDevice((prev) => mergeNewer(prev, [pos]));
          } catch {
            // A single malformed event must not bring down the whole stream --
            // drop it and keep listening.
          }
        };
        es.onerror = () => {
          es.close();
          if (esRef.current === es) esRef.current = null;
          scheduleReconnect();
        };
      } catch {
        // Failure minting the ticket or in the initial seed (network down,
        // expired session, etc.) -- same treatment as an error on an open
        // stream: retry with backoff, never hang the UI.
        scheduleReconnect();
      }
    }

    activeStreamCloser = () => {
      stoppedRef.current = true;
      if (reconnectTimeoutRef.current) clearTimeout(reconnectTimeoutRef.current);
      esRef.current?.close();
      esRef.current = null;
    };

    openStream();

    // Low-frequency reconciliation -- see the module docstring. Always runs,
    // independent of the stream state.
    const reconciliationInterval = setInterval(async () => {
      try {
        const seed = await api.latestPositions();
        if (!stoppedRef.current) setPositionByDevice((prev) => mergeNewer(prev, seed));
      } catch {
        // Silent -- a background safety net, not the main data source; a
        // transient error must not interrupt anything visible.
      }
    }, RECONCILIATION_POLL_MS);

    return () => {
      activeStreamCloser?.();
      activeStreamCloser = null;
      clearInterval(reconciliationInterval);
    };
  }, []);

  return {
    positions: Array.from(positionByDevice.values()),
    deviceStatusByDevice,
    connectionState,
  };
}
