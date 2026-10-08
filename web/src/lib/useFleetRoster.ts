import { useEffect, useMemo, useState } from "react";
import { api, ApiError, type AlarmSeverity, type Device, type DevicePosition, type Tenant, type Vehicle } from "./api";
import { useUnacknowledgedAlarms } from "./useUnacknowledgedAlarms";
import { useLivePositions, type LiveConnectionState } from "./useLivePositions";

// Fleet roster (devices/tenants/vehicles/live positions/alarm severity per
// device) -- shared by MapView.tsx and the Units view without duplicating
// fetch/polling. A single place that knows how to load the fleet; any new screen
// reuses it.
const ROSTER_POLL_MS = 60_000;

const SEVERITY_RANK: Record<AlarmSeverity, number> = { critical: 3, warning: 2, info: 1 };

export function useFleetRoster() {
  const [devices, setDevices] = useState<Device[]>([]);
  const [tenants, setTenants] = useState<Tenant[]>([]);
  const [vehicles, setVehicles] = useState<Vehicle[]>([]);
  const [error, setError] = useState<string | null>(null);
  const unacknowledgedAlarms = useUnacknowledgedAlarms();
  const { positions, deviceStatusByDevice, connectionState } = useLivePositions();

  async function poll() {
    try {
      // limit: 1000 -- a pragmatic ceiling, not a real scaling solution (see the
      // pagination section of web/README.md). The map/unit views need the full
      // roster to resolve marker/card metadata, not just one page.
      const [devicesData, tenantsData, vehiclesData] = await Promise.all([
        api.listDevices({ limit: 1000, exclude_inactive: true }),
        api.listTenants({ limit: 1000 }),
        api.listVehicles({ limit: 1000 }),
      ]);
      setDevices(devicesData.items);
      setTenants(tenantsData.items);
      setVehicles(vehiclesData.items);
      setError(null);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "error cargando la flota");
    }
  }

  useEffect(() => {
    let cancelled = false;
    (async () => {
      if (!cancelled) await poll();
    })();
    const interval = setInterval(poll, ROSTER_POLL_MS);
    return () => {
      cancelled = true;
      clearInterval(interval);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const severityByDevice = useMemo(() => {
    const map = new Map<string, AlarmSeverity>();
    for (const a of unacknowledgedAlarms) {
      const current = map.get(a.device_id);
      if (!current || SEVERITY_RANK[a.severity] > SEVERITY_RANK[current]) map.set(a.device_id, a.severity);
    }
    return map;
  }, [unacknowledgedAlarms]);

  // Only positions of units in the roster (which already excludes deactivated
  // ones): a deactivated unit must neither appear nor move the map's framing.
  // While the roster loads, all positions are used.
  const rosterPositions = useMemo(() => {
    if (devices.length === 0) return positions;
    const ids = new Set(devices.map((d) => d.id));
    return positions.filter((p) => ids.has(p.device_id));
  }, [positions, devices]);
  const positionByDevice = useMemo(() => new Map(rosterPositions.map((p) => [p.device_id, p])), [rosterPositions]);
  const tenantById = useMemo(() => new Map(tenants.map((t) => [t.id, t])), [tenants]);
  // Overlays live ignition/power (SSE push, see useLivePositions.ts) on top of
  // the REST roster, so an ignition change shows without reloading, just like
  // positions. ignition_changed_at/power_changed_at ("X ago" in
  // DeviceDetailPanel) also travel in the payload (migration 0049) -- otherwise
  // the icon would update live while "X ago" still showed the stale value from
  // the last REST poll.
  const liveDevices = useMemo(() => {
    if (deviceStatusByDevice.size === 0) return devices;
    return devices.map((d) => {
      const live = deviceStatusByDevice.get(d.id);
      if (!live) return d;
      return {
        ...d,
        ignition_on: live.ignition_on,
        power_connected: live.power_connected,
        ignition_changed_at: live.ignition_changed_at,
        power_changed_at: live.power_changed_at,
      };
    });
  }, [devices, deviceStatusByDevice]);
  const deviceById = useMemo(() => new Map(liveDevices.map((d) => [d.id, d])), [liveDevices]);
  const vehicleById = useMemo(() => new Map(vehicles.map((v) => [v.id, v])), [vehicles]);

  return {
    devices: liveDevices,
    tenants,
    vehicles,
    positions: rosterPositions,
    connectionState,
    error,
    refresh: poll,
    severityByDevice,
    positionByDevice,
    tenantById,
    deviceById,
    vehicleById,
  };
}

export type FleetRoster = ReturnType<typeof useFleetRoster>;
export type { DevicePosition, LiveConnectionState };
