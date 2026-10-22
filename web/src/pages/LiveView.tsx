import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { api, ApiError, hasCamera, type Device } from "../lib/api";
import { Alert, EmptyState, PageContainer, PageHeader } from "../components/ui";
import { CameraTile } from "../components/CameraTile";
import { useFloatingCameras } from "../lib/floatingCameras";

export default function LiveView() {
  const { openCamera } = useFloatingCameras();
  const { t } = useTranslation();
  const [devices, setDevices] = useState<Device[]>([]);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api
      .listDevices({ limit: 1000, exclude_inactive: true })
      .then(({ items }) => {
        const cameras = items.filter((d) => hasCamera(d.protocol));
        setDevices(cameras);
        setSelected(new Set(cameras.slice(0, 2).map((x) => x.id)));
      })
      .catch((err) => setError(err instanceof ApiError ? err.message : t("live.error_loading")));
  }, []);

  function toggle(id: string) {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  const cameraFront = t("clips.camera_front");
  const cameraCabin = t("clips.camera_cabin");

  return (
    <PageContainer wide>
      <PageHeader title={t("live.title")} description={t("live.description")} />

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
              d.protocol === "gt06_video"
                ? [
                    <CameraTile key={`${d.id}-0`} deviceId={d.id} label={`${d.label} · ${cameraFront}`} channel={0} protocol={d.protocol} onPopOut={() => openCamera({ deviceId: d.id, channel: 0, protocol: d.protocol, label: `${d.label} · ${cameraFront}` }, { mode: "floating" })} />,
                    <CameraTile key={`${d.id}-1`} deviceId={d.id} label={`${d.label} · ${cameraCabin}`} channel={1} protocol={d.protocol} onPopOut={() => openCamera({ deviceId: d.id, channel: 1, protocol: d.protocol, label: `${d.label} · ${cameraCabin}` }, { mode: "floating" })} />,
                  ]
                : [<CameraTile key={d.id} deviceId={d.id} label={d.label} protocol={d.protocol} onPopOut={() => openCamera({ deviceId: d.id, protocol: d.protocol, label: d.label }, { mode: "floating" })} />]
            )}
        </div>
      )}
    </PageContainer>
  );
}
