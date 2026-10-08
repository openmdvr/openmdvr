import { useNavigate } from "react-router-dom";
import { useFleetRoster } from "../lib/useFleetRoster";
import { DeviceListPanel } from "../components/DeviceListPanel";
import { Alert } from "../components/ui";

// "Units" tab (mobile): direct access to the whole fleet. Tapping a unit opens
// its card; each row's camera button opens the camera right there, without
// navigating (see lib/floatingCameras.tsx).
export default function UnitsView() {
  const navigate = useNavigate();
  const { devices, vehicleById, positionByDevice, severityByDevice, error } = useFleetRoster();

  return (
    <div className="flex h-full flex-col">
      <div className="px-4 pt-1">
        <h1 className="text-xl font-semibold tracking-tight text-ink">Unidades</h1>
      </div>
      {error && (
        <div className="px-4 pt-2">
          <Alert>{error}</Alert>
        </div>
      )}
      <div className="min-h-0 flex-1">
        <DeviceListPanel
          devices={devices}
          vehicleById={vehicleById}
          positionByDevice={positionByDevice}
          severityByDevice={severityByDevice}
          selectedId={null}
          onSelect={(id) => navigate(`/units/${id}`)}
        />
      </div>
    </div>
  );
}
