import { useParams, useSearchParams, Link } from "react-router-dom";
import { CameraTile } from "../components/CameraTile";
import type { DeviceProtocol } from "../lib/api";
import { PageContainer } from "../components/ui";

// Single-camera live view. The player logic lives in components/CameraTile.tsx
// (WebRTC/WHEP, low latency), shared with the multi-camera view (LiveView.tsx).
export default function DeviceVideo() {
  const { deviceId } = useParams<{ deviceId: string }>();
  const [searchParams] = useSearchParams();
  // ?protocol= comes from the link that leads here (Dashboard.tsx, Devices
  // table) -- this page never loads the full device itself (there is no
  // single-device GET /devices/{id} today), so it is the cheapest way to know
  // whether the default preview photo applies (see CameraTile.tsx) without
  // adding a fetch just for this screen.
  const protocol = (searchParams.get("protocol") as DeviceProtocol | null) ?? undefined;
  if (!deviceId) return null;

  return (
    <PageContainer>
      <Link to="/live" className="text-sm font-medium text-brand-700 hover:underline">
        ← En vivo
      </Link>
      <div className="max-w-2xl">
        <CameraTile deviceId={deviceId} protocol={protocol} />
      </div>
    </PageContainer>
  );
}
