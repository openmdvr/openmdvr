import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from "react";
import type { DeviceProtocol } from "./api";
import { useAuth } from "./auth";

// Open cameras: DOCK by default, floating window optional.
//
// Opening a camera from the map or the unit list puts it in a DOCK below the
// content (components/CameraDock.tsx) so the map stays visible, and it starts
// with the preview PHOTO, never live video (video costs a lot of data; it is
// requested with a click). Popping it out to a floating window is optional
// (button in the dock), and it can be sent back to the dock.
//
// Floating windows are draggable and resizable, SURVIVE navigation (the provider
// lives above <Routes>, see App.tsx), and can be moved out of the browser into
// an always-on-top OS window (Document Picture-in-Picture, see
// components/FloatingCameras.tsx).
//
// Security: no new path to the video. Each window is the same CameraTile -- same
// one-time ticket, same per-session limits and monthly bridge quota, same RLS
// when requesting the stream. The only thing persisted locally is WHICH windows
// were open (ids + position), per user (key includes userId), and it is cleared
// on logout: another user in the same browser never inherits the previous user's
// windows (and even if they did, the API would reject the stream through RLS).

export type CameraMode = "dock" | "floating";

export interface FloatingCamera {
  key: string;
  // Where it lives: in the dock (default) or as a floating window.
  mode: CameraMode;
  deviceId: string;
  channel?: number;
  label: string;
  protocol?: DeviceProtocol;
  x: number;
  y: number;
  width: number;
  minimized: boolean;
  z: number;
  // true only for windows opened since this page load with the intent to watch
  // live; when restored after a reload they start with the preview photo (a
  // stream that costs data is not resumed without a new click).
  autoStart: boolean;
  // Incremented each time an ALREADY open window is requested again (clicking
  // the camera icon again) -- CameraTile uses it to restart a video that already
  // ended (ended/error) instead of only bringing the window to the front.
  // Without it, after a time-limit cut, reopening two cameras would only restart
  // the one that had been closed.
  startNonce?: number;
}

export type OpenCameraInput = Pick<FloatingCamera, "deviceId" | "channel" | "label" | "protocol">;

export interface OpenCameraOptions {
  // Default "dock".
  mode?: CameraMode;
  // true = start live video right away. Default false: the preview photo is
  // shown first (data saving).
  live?: boolean;
}

interface FloatingCamerasValue {
  cameras: FloatingCamera[];
  openCamera: (input: OpenCameraInput, options?: OpenCameraOptions) => void;
  closeCamera: (key: string) => void;
  closeAll: () => void;
  setMode: (key: string, mode: CameraMode) => void;
  updateCamera: (key: string, patch: Partial<FloatingCamera>) => void;
  focusCamera: (key: string) => void;
  isOpen: (deviceId: string, channel?: number) => boolean;
}

// Total cap (dock + floating): each live camera costs real data.
const MAX_WINDOWS = 8;
const Ctx = createContext<FloatingCamerasValue | null>(null);

export function cameraKey(deviceId: string, channel?: number) {
  return `${deviceId}:${channel ?? "d"}`;
}

function storageKey(userId: string | null) {
  return userId ? `omd-floating-cams:${userId}` : null;
}

function load(userId: string | null): FloatingCamera[] {
  const key = storageKey(userId);
  if (!key) return [];
  try {
    const parsed = JSON.parse(localStorage.getItem(key) ?? "[]");
    if (!Array.isArray(parsed)) return [];
    return parsed
      .filter((c) => c && typeof c.deviceId === "string" && typeof c.label === "string")
      .slice(0, MAX_WINDOWS)
      .map((c) => ({ ...c, mode: c.mode === "floating" ? "floating" : "dock", autoStart: false }));
  } catch {
    return [];
  }
}

function defaultGeometry(index: number): Pick<FloatingCamera, "x" | "y" | "width"> {
  const vw = typeof window !== "undefined" ? window.innerWidth : 1280;
  const vh = typeof window !== "undefined" ? window.innerHeight : 800;
  const width = Math.min(360, Math.round(vw * 0.78));
  const height = Math.round(width * 0.62) + 40;
  const offset = index * 28;
  return {
    width,
    x: Math.max(8, vw - width - 20 - offset),
    y: Math.max(8, vh - height - (vw < 768 ? 96 : 24) - offset),
  };
}

export function FloatingCamerasProvider({ children }: { children: ReactNode }) {
  const { userId, token } = useAuth();
  const [cameras, setCameras] = useState<FloatingCamera[]>(() => load(userId));

  // User switch / logout: never carry over someone else's windows.
  useEffect(() => {
    setCameras(token ? load(userId) : []);
  }, [userId, token]);

  useEffect(() => {
    const key = storageKey(userId);
    if (!key || !token) return;
    try {
      localStorage.setItem(key, JSON.stringify(cameras));
    } catch {
      // per-browser convenience; without storage windows live in memory
    }
  }, [cameras, userId, token]);

  // Remove the legacy Map tray key (list of ids without labels).
  useEffect(() => {
    try {
      localStorage.removeItem("omd-live-tray");
    } catch {
      // nothing
    }
  }, []);

  const openCamera = useCallback((input: OpenCameraInput, options?: OpenCameraOptions) => {
    const live = options?.live ?? false;
    setCameras((prev) => {
      const key = cameraKey(input.deviceId, input.channel);
      const topZ = prev.reduce((m, c) => Math.max(m, c.z), 0) + 1;
      const existing = prev.find((c) => c.key === key);
      if (existing)
        // Already open: bring it into view. Only restart the video if live was
        // explicitly requested (never just because it was opened again).
        return prev.map((c) =>
          c.key === key
            ? {
                ...c,
                mode: options?.mode ?? c.mode,
                minimized: false,
                z: topZ,
                ...(live ? { autoStart: true, startNonce: (c.startNonce ?? 0) + 1 } : {}),
              }
            : c,
        );
      const kept = prev.length >= MAX_WINDOWS ? prev.slice(1) : prev;
      const floatingCount = kept.filter((c) => c.mode === "floating").length;
      return [
        ...kept,
        { ...input, key, mode: options?.mode ?? "dock", ...defaultGeometry(floatingCount), minimized: false, z: topZ, autoStart: live },
      ];
    });
  }, []);

  const closeCamera = useCallback((key: string) => setCameras((prev) => prev.filter((c) => c.key !== key)), []);
  const closeAll = useCallback(() => setCameras([]), []);
  const setMode = useCallback(
    (key: string, mode: CameraMode) =>
      setCameras((prev) => {
        const topZ = prev.reduce((m, c) => Math.max(m, c.z), 0) + 1;
        const floatingCount = prev.filter((c) => c.mode === "floating" && c.key !== key).length;
        return prev.map((c) =>
          c.key === key ? { ...c, mode, minimized: false, z: topZ, ...(mode === "floating" ? defaultGeometry(floatingCount) : {}) } : c,
        );
      }),
    [],
  );
  const updateCamera = useCallback(
    (key: string, patch: Partial<FloatingCamera>) => setCameras((prev) => prev.map((c) => (c.key === key ? { ...c, ...patch } : c))),
    [],
  );
  const focusCamera = useCallback(
    (key: string) =>
      setCameras((prev) => {
        const topZ = prev.reduce((m, c) => Math.max(m, c.z), 0);
        const target = prev.find((c) => c.key === key);
        if (!target || target.z === topZ) return prev;
        return prev.map((c) => (c.key === key ? { ...c, z: topZ + 1 } : c));
      }),
    [],
  );

  const value = useMemo<FloatingCamerasValue>(
    () => ({
      cameras,
      openCamera,
      closeCamera,
      closeAll,
      setMode,
      updateCamera,
      focusCamera,
      isOpen: (deviceId, channel) => cameras.some((c) => c.key === cameraKey(deviceId, channel)),
    }),
    [cameras, openCamera, closeCamera, closeAll, setMode, updateCamera, focusCamera],
  );

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useFloatingCameras(): FloatingCamerasValue {
  const v = useContext(Ctx);
  if (!v) throw new Error("useFloatingCameras fuera de FloatingCamerasProvider");
  return v;
}
