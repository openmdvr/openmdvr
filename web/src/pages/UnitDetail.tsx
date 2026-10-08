import { useNavigate, useParams } from "react-router-dom";
import { useFleetRoster } from "../lib/useFleetRoster";
import { DeviceDetailPanel } from "../components/DeviceDetailPanel";
import { Alert, Button, EmptyState } from "../components/ui";

// Full card of ONE unit (mobile: from Units or from the map). Reuses
// DeviceDetailPanel as-is.
//
// "View trail" navigates to the Map with the unit selected and its last-hour
// trail already drawn (?device=&trail=1, see MapView.tsx), since this page has
// no map of its own.
export default function UnitDetail() {
  const { deviceId } = useParams<{ deviceId: string }>();
  const navigate = useNavigate();
  const { error, deviceById, vehicleById, tenantById, positionByDevice, severityByDevice } = useFleetRoster();

  if (!deviceId) return null;
  const device = deviceById.get(deviceId) ?? null;

  return (
    <div className="mx-auto flex h-full max-w-2xl flex-col">
      {error && (
        <div className="px-4 pt-2">
          <Alert>{error}</Alert>
        </div>
      )}
      {!device ? (
        <EmptyState>Unidad no encontrada.</EmptyState>
      ) : (
        <>
          <div className="px-4 pt-1">
            <Button variant="secondary" className="w-full" onClick={() => navigate(`/map?device=${device.id}`)}>
              Seguir en el mapa
            </Button>
          </div>
          <div className="min-h-0 flex-1">
            <DeviceDetailPanel
              device={device}
              vehicle={device.vehicle_id ? (vehicleById.get(device.vehicle_id) ?? null) : null}
              position={positionByDevice.get(device.id) ?? null}
              tenant={tenantById.get(device.tenant_id) ?? null}
              hasAlarm={severityByDevice.has(device.id)}
              trailVisible={false}
              onToggleTrail={() => navigate(`/map?device=${device.id}&trail=1`)}
              onClose={() => navigate("/units")}
            />
          </div>
        </>
      )}
    </div>
  );
}
