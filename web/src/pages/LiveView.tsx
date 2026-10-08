import { useEffect, useState } from "react";
import { api, ApiError, hasCamera, type Device } from "../lib/api";
import { Alert, EmptyState, PageContainer, PageHeader } from "../components/ui";
import { CameraTile } from "../components/CameraTile";
import { useFloatingCameras } from "../lib/floatingCameras";

export default function LiveView() {
  const { openCamera } = useFloatingCameras();
  const [devices, setDevices] = useState<Device[]>([]);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    // limit: 1000 -- same pragmatic ceiling as MapView.tsx, not a real scaling
    // solution for this grid if a customer reaches thousands of units (see the
    // pagination section of web/README.md).
    api
      .listDevices({ limit: 1000, exclude_inactive: true })
      .then(({ items }) => {
        // A GPS-only tracker (protocol "gt06", NOT "gt06_video") has no camera
        // -- this view is video-only, so offering it makes no sense (selecting
        // it would hit the backend's 400).
        const cameras = items.filter((d) => hasCamera(d.protocol));
        setDevices(cameras);
        // Selects up to 2 cameras by default -- "watch several cameras at once"
        // is this view's core use case, not something the user should build from
        // scratch on every visit.
        setSelected(new Set(cameras.slice(0, 2).map((x) => x.id)));
      })
      .catch((err) => setError(err instanceof ApiError ? err.message : "error cargando dispositivos"));
  }, []);

  function toggle(id: string) {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  return (
    <PageContainer wide>
      <PageHeader title="En vivo" description="Selecciona las cámaras que quieres ver al mismo tiempo." />

      {error && <Alert>{error}</Alert>}

      <div className="flex flex-wrap gap-2">
        {devices.map((d) => (
          <button
            key={d.id}
            onClick={() => toggle(d.id)}
            className={`rounded-sm border px-3 py-1.5 text-sm font-medium transition-colors ${
              selected.has(d.id)
                ? "border-brand-600 bg-brand-600 text-white"
                : "border-line-strong bg-surface text-ink-dim hover:border-brand-600"
            }`}
          >
            {d.label}
          </button>
        ))}
      </div>

      {selected.size === 0 ? (
        <EmptyState>Selecciona al menos una cámara para verla en vivo.</EmptyState>
      ) : (
        <div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-3 2xl:grid-cols-4">
          {devices
            .filter((d) => selected.has(d.id))
            .flatMap((d) =>
              // The JC261/JC400 (protocol gt06_video) has TWO independent
              // physical cameras (Front/Cabin, see DeviceDetailPanel.tsx) -- a
              // <CameraTile> without `channel` falls back to the default
              // (channel 1, "Cabin"), which would hide the Front camera here
              // entirely. Each tile still starts "idle" (never requests video
              // until the user clicks "watch live") -- the extra tile costs no
              // real bandwidth, it only makes the camera discoverable.
              d.protocol === "gt06_video"
                ? [
                    <CameraTile key={`${d.id}-0`} deviceId={d.id} label={`${d.label} · Frontal`} channel={0} protocol={d.protocol} onPopOut={() => openCamera({ deviceId: d.id, channel: 0, protocol: d.protocol, label: `${d.label} · Frontal` }, { mode: "floating" })} />,
                    <CameraTile key={`${d.id}-1`} deviceId={d.id} label={`${d.label} · Cabina`} channel={1} protocol={d.protocol} onPopOut={() => openCamera({ deviceId: d.id, channel: 1, protocol: d.protocol, label: `${d.label} · Cabina` }, { mode: "floating" })} />,
                  ]
                : [<CameraTile key={d.id} deviceId={d.id} label={d.label} protocol={d.protocol} onPopOut={() => openCamera({ deviceId: d.id, protocol: d.protocol, label: d.label }, { mode: "floating" })} />]
            )}
        </div>
      )}
    </PageContainer>
  );
}
