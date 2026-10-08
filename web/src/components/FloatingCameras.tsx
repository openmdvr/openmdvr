import { useEffect, useRef, useState, type PointerEvent as ReactPointerEvent } from "react";
import { useAuth } from "../lib/auth";
import { useFloatingCameras, type FloatingCamera } from "../lib/floatingCameras";
import { CameraTile } from "./CameraTile";
import { CloseIcon, DockIcon, PopOutIcon } from "./icons";

// Floating camera window layer (see lib/floatingCameras.tsx).
//
// Positioning: the layer is `absolute inset-0` inside the app root container
// (App.tsx), never position:fixed. pointer-events-none on the layer and -auto on
// each window: outside a window, taps reach the map/page normally.
//
// "Outside the page": Document Picture-in-Picture (Chrome/Edge 116+) moves the
// window's DOM node -- with its WebRTC <video> already playing -- into an
// always-on-top OS window, even if the user switches tabs or apps. Same React
// instance and same connection: no new stream or ticket. Where that API does not
// exist (Safari/Firefox, mobile) the native <video> PiP is used.

interface DocumentPictureInPicture {
  requestWindow(options?: { width?: number; height?: number }): Promise<Window>;
}
declare global {
  interface Window {
    documentPictureInPicture?: DocumentPictureInPicture;
  }
}

const MIN_WIDTH = 220;
const HEADER_H = 40;

function copyStylesInto(target: Document) {
  for (const node of Array.from(document.head.querySelectorAll("style, link[rel='stylesheet']"))) {
    target.head.appendChild(node.cloneNode(true));
  }
  target.documentElement.className = document.documentElement.className;
  target.body.style.margin = "0";
  target.body.style.background = "#000";
}

function IconBtn({ label, onClick, children }: { label: string; onClick: () => void; children: React.ReactNode }) {
  return (
    <button
      type="button"
      title={label}
      aria-label={label}
      onPointerDown={(e) => e.stopPropagation()}
      onClick={onClick}
      className="flex h-7 w-7 items-center justify-center rounded-lg text-ink-dim transition-colors hover:bg-fg/10 hover:text-ink"
    >
      {children}
    </button>
  );
}

function CameraWindow({ cam }: { cam: FloatingCamera }) {
  const { closeCamera, updateCamera, focusCamera, setMode } = useFloatingCameras();
  const contentRef = useRef<HTMLDivElement>(null);
  const bodySlotRef = useRef<HTMLDivElement>(null);
  const pipRef = useRef<Window | null>(null);
  const [poppedOut, setPoppedOut] = useState(false);
  const height = Math.round(cam.width * 0.5625);

  // Keep the window inside the viewport when the phone rotates or the browser is
  // resized.
  useEffect(() => {
    const clamp = () => {
      const maxX = Math.max(0, window.innerWidth - cam.width - 8);
      const maxY = Math.max(0, window.innerHeight - HEADER_H - 8);
      if (cam.x > maxX || cam.y > maxY) updateCamera(cam.key, { x: Math.min(cam.x, maxX), y: Math.min(cam.y, maxY) });
    };
    clamp();
    window.addEventListener("resize", clamp);
    return () => window.removeEventListener("resize", clamp);
  }, [cam.key, cam.x, cam.y, cam.width, updateCamera]);

  // Closing the floating window also closes its external window.
  useEffect(() => () => pipRef.current?.close(), []);

  function startDrag(e: ReactPointerEvent) {
    if (e.button !== 0 && e.pointerType === "mouse") return;
    focusCamera(cam.key);
    const startX = e.clientX;
    const startY = e.clientY;
    const originX = cam.x;
    const originY = cam.y;
    const target = e.currentTarget as HTMLElement;
    target.setPointerCapture(e.pointerId);
    const move = (ev: PointerEvent) => {
      const x = Math.min(Math.max(0, originX + ev.clientX - startX), window.innerWidth - 80);
      const y = Math.min(Math.max(0, originY + ev.clientY - startY), window.innerHeight - HEADER_H);
      updateCamera(cam.key, { x, y });
    };
    const up = () => {
      target.removeEventListener("pointermove", move);
      target.removeEventListener("pointerup", up);
      target.removeEventListener("pointercancel", up);
    };
    target.addEventListener("pointermove", move);
    target.addEventListener("pointerup", up);
    target.addEventListener("pointercancel", up);
  }

  function startResize(e: ReactPointerEvent) {
    e.stopPropagation();
    focusCamera(cam.key);
    const startX = e.clientX;
    const originW = cam.width;
    const target = e.currentTarget as HTMLElement;
    target.setPointerCapture(e.pointerId);
    const move = (ev: PointerEvent) => {
      const maxW = window.innerWidth - cam.x - 8;
      updateCamera(cam.key, { width: Math.round(Math.min(Math.max(MIN_WIDTH, originW + ev.clientX - startX), maxW, 960)) });
    };
    const up = () => {
      target.removeEventListener("pointermove", move);
      target.removeEventListener("pointerup", up);
    };
    target.addEventListener("pointermove", move);
    target.addEventListener("pointerup", up);
  }

  async function popOut() {
    const content = contentRef.current;
    if (!content) return;
    const dpip = window.documentPictureInPicture;
    if (dpip) {
      try {
        const pip = await dpip.requestWindow({ width: Math.max(cam.width, 420), height: Math.round(Math.max(cam.width, 420) * 0.5625) + 44 });
        copyStylesInto(pip.document);
        pip.document.title = cam.label;
        pip.document.body.appendChild(content);
        content.style.height = "100vh";
        pipRef.current = pip;
        setPoppedOut(true);
        pip.addEventListener("pagehide", () => {
          content.style.height = "";
          const slot = bodySlotRef.current;
          if (slot) slot.insertBefore(content, slot.firstChild);
          pipRef.current = null;
          setPoppedOut(false);
        });
        return;
      } catch (err) {
        console.warn("[cameras] Document PiP unavailable, falling back to video PiP", err);
      }
    }
    const video = content.querySelector("video");
    if (video && document.pictureInPictureEnabled && video.readyState >= 2) {
      try {
        await video.requestPictureInPicture();
      } catch (err) {
        console.warn("[cameras] video PiP rejected", err);
      }
    }
  }

  return (
    <div
      className="glass pointer-events-auto absolute flex flex-col overflow-hidden rounded-2xl"
      style={{ left: cam.x, top: cam.y, width: cam.width, zIndex: 10 + cam.z }}
      onPointerDown={() => focusCamera(cam.key)}
    >
      <div
        className="flex h-10 shrink-0 cursor-grab touch-none items-center gap-2 pr-1 pl-3 select-none active:cursor-grabbing"
        onPointerDown={startDrag}
      >
        <span className="h-2 w-2 shrink-0 rounded-full bg-rose-500" aria-hidden />
        <span className="min-w-0 flex-1 truncate text-[13px] font-semibold text-ink">{cam.label}</span>
        <IconBtn label="Regresar a la cinta" onClick={() => setMode(cam.key, "dock")}>
          <DockIcon size={15} />
        </IconBtn>
        <IconBtn label="Sacar a una ventana externa" onClick={popOut}>
          <PopOutIcon size={15} />
        </IconBtn>
        <IconBtn label={cam.minimized ? "Restaurar" : "Minimizar"} onClick={() => updateCamera(cam.key, { minimized: !cam.minimized })}>
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
            {cam.minimized ? <rect x="5" y="5" width="14" height="14" rx="2" /> : <path d="M6 12h12" strokeLinecap="round" />}
          </svg>
        </IconBtn>
        <IconBtn label="Cerrar" onClick={() => closeCamera(cam.key)}>
          <CloseIcon size={14} />
        </IconBtn>
      </div>
      {/*
       * Content is NEVER unmounted when minimized (only hidden), so the stream
       * is neither cut nor re-requested on restore.
       */}
      <div ref={bodySlotRef} className={cam.minimized ? "hidden" : "relative"} style={{ height: poppedOut ? 64 : height }}>
        <div ref={contentRef} className="h-full">
          <CameraTile deviceId={cam.deviceId} channel={cam.channel} protocol={cam.protocol} label={cam.label} autoStart={cam.autoStart} restartSignal={cam.startNonce} bare />
        </div>
        {poppedOut && (
          <div className="flex h-full items-center justify-between gap-2 px-3 text-xs text-ink-dim">
            <span>Reproduciendo en una ventana externa</span>
            <button onClick={() => pipRef.current?.close()} className="rounded-lg bg-fg/10 px-2 py-1 font-medium text-ink hover:bg-fg/20">
              Traer de vuelta
            </button>
          </div>
        )}
        {!poppedOut && (
          <span
            onPointerDown={startResize}
            className="absolute right-0 bottom-0 z-40 h-5 w-5 cursor-nwse-resize touch-none"
            aria-label="Redimensionar"
            role="separator"
          >
            <svg viewBox="0 0 20 20" className="h-full w-full text-white/50">
              <path d="M17 8v9H8M17 13v4h-4" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />
            </svg>
          </span>
        )}
      </div>
    </div>
  );
}

export function FloatingCameraLayer() {
  const { token, isDriver } = useAuth();
  const { cameras } = useFloatingCameras();
  const floating = cameras.filter((c) => c.mode === "floating");
  if (!token || isDriver || floating.length === 0) return null;
  return (
    <div className="pointer-events-none absolute inset-0 z-[3000] overflow-hidden" aria-label="Cámaras flotantes">
      {floating.map((cam) => (
        <CameraWindow key={cam.key} cam={cam} />
      ))}
    </div>
  );
}
