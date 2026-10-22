import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import i18next from "i18next";
import mpegts from "mpegts.js";
import type { AlarmVideoClip, DeviceProtocol } from "../lib/api";

function ClipVideo({ url, label }: { url: string; label?: string }) {
  const videoRef = useRef<HTMLVideoElement>(null);
  const { t } = useTranslation();
  const [error, setError] = useState<string | null>(null);
  const [ready, setReady] = useState(false);

  useEffect(() => {
    setReady(false);
    setError(null);
    const videoEl = videoRef.current;
    if (!videoEl) return;
    if (!mpegts.isSupported()) {
      setError(t("clips.no_mse"));
      return;
    }
    const player = mpegts.createPlayer({ type: "mpegts", url, isLive: false });
    player.on(mpegts.Events.ERROR, () => setError(t("clips.error_expired")));
    const onReady = () => setReady(true);
    videoEl.addEventListener("loadeddata", onReady);
    player.attachMediaElement(videoEl);
    player.load();
    return () => {
      videoEl.removeEventListener("loadeddata", onReady);
      player.destroy();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [url]);

  return (
    <div className="overflow-hidden rounded-sm border border-line bg-black">
      {label && (
        <div className="border-b border-line bg-surface-2 px-2 py-1 text-xs font-medium text-ink-dim">{label}</div>
      )}
      <div className="relative aspect-video w-full">
        {error ? (
          <p className="flex h-full items-center justify-center p-3 text-center text-xs text-red-400">{error}</p>
        ) : (
          <>
            {!ready && (
              <p className="absolute inset-0 flex items-center justify-center text-xs text-ink-faint">
                {t("clips.loading")}
              </p>
            )}
            <video
              ref={videoRef}
              controls
              playsInline
              className={`h-full w-full transition-opacity duration-150 ${ready ? "opacity-100" : "opacity-0"}`}
            />
          </>
        )}
      </div>
    </div>
  );
}

export function AlarmClipPlayer({ url, secondaryUrl }: { url: string; secondaryUrl?: string | null }) {
  const { t } = useTranslation();
  if (secondaryUrl) {
    return (
      <div className="grid max-w-2xl grid-cols-1 gap-3 sm:grid-cols-2">
        <ClipVideo url={url} label={t("clips.camera_front")} />
        <ClipVideo url={secondaryUrl} label={t("clips.camera_cabin")} />
      </div>
    );
  }
  return (
    <div className="max-w-lg">
      <ClipVideo url={url} />
    </div>
  );
}

export const CLIP_SUPPORTED_PROTOCOL: DeviceProtocol = "gt06_video";

export const CLIP_CAPABLE_ALARM_TYPES: ReadonlySet<string> = new Set(["gt06_camera_event"]);

export function alarmCanHaveClip(protocol: DeviceProtocol | undefined, alarmType: string | null | undefined): boolean {
  return protocol === CLIP_SUPPORTED_PROTOCOL && !!alarmType && CLIP_CAPABLE_ALARM_TYPES.has(alarmType);
}

export function clipStatusLabel(status: AlarmVideoClip["status"]): string {
  return i18next.t(`clips.status_${status}`, { defaultValue: status });
}

export const CLIP_STATUS_LABEL: Record<AlarmVideoClip["status"], string> = {
  requested: "pidiendo clip...",
  uploading: "subiendo clip...",
  ready: "clip listo",
  failed: "no se pudo obtener el clip",
  unsupported: "este dispositivo no soporta clips",
};
