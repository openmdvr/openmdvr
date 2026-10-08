import { Circle, Polygon, Tooltip } from "react-leaflet";
import type { Geofence } from "../lib/api";

// Reusable geofence layer, mounted INSIDE any <MapContainer> (Geofences page,
// live Map, Route history). Uses Leaflet's native vector primitives
// (Circle/Polygon, rendered as SVG) -- no new dependency, light even with
// hundreds of geofences.
//
// Disabled = dashed stroke and nearly transparent fill, so it is obvious at a
// glance that it is NOT generating events.

interface Props {
  geofences: Geofence[];
  selectedId?: string | null;
  onSelect?: (g: Geofence) => void;
  // Always shows the name (permanent label) instead of only on hover -- useful
  // on the Geofences page with few geofences.
  permanentLabels?: boolean;
}

export function GeofenceLayer({ geofences, selectedId = null, onSelect, permanentLabels = false }: Props) {
  return (
    <>
      {geofences.map((g) => {
        const selected = g.id === selectedId;
        const pathOptions = {
          color: g.color,
          weight: selected ? 3 : 2,
          opacity: g.enabled ? 0.9 : 0.5,
          fillColor: g.color,
          fillOpacity: g.enabled ? (selected ? 0.28 : 0.14) : 0.04,
          dashArray: g.enabled ? undefined : "6 6",
        };
        const handlers = onSelect ? { click: () => onSelect(g) } : undefined;
        const label = (
          <Tooltip direction="center" permanent={permanentLabels || selected} className="geofence-label" opacity={1}>
            {g.name}
          </Tooltip>
        );
        if (g.shape === "circle" && g.center_lat != null && g.center_lon != null && g.radius_m != null) {
          return (
            <Circle
              key={`${g.id}-${g.updated_at}`}
              center={[g.center_lat, g.center_lon]}
              radius={g.radius_m}
              pathOptions={pathOptions}
              eventHandlers={handlers}
            >
              {label}
            </Circle>
          );
        }
        if (g.shape === "polygon" && g.polygon) {
          return (
            <Polygon key={`${g.id}-${g.updated_at}`} positions={g.polygon} pathOptions={pathOptions} eventHandlers={handlers}>
              {label}
            </Polygon>
          );
        }
        return null;
      })}
    </>
  );
}
