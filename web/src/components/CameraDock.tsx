import { useEffect, useRef, useState } from "react";
import { formatQuota } from "../lib/duration";
import { useFloatingCameras, type FloatingCamera } from "../lib/floatingCameras";
import { useLiveBalance } from "../lib/liveUsage";
import { useIsMobile } from "../lib/useIsMobile";
import { CameraTile } from "./CameraTile";
import { CameraIcon, ChevronDownIcon, ChevronUpIcon, CloseIcon, ExpandIcon, PopOutIcon, ShrinkIcon } from "./icons";

// Camera dock: cameras opened from the map or the unit list are placed in a
// strip below the content so the map stays visible; popping them out to a
// floating window is optional (button on each card).
//
// Lives IN FLOW inside the Layout (between the page and the tab bar on mobile),
// never position:fixed.
//
// Data saving: each card starts with the preview photo; live video is requested
// with a click. Minimizing the dock UNMOUNTS the cards, so any live video stops
// (and stops consuming quota) instead of running hidden.

const COLLAPSED_KEY = "omd-dock-collapsed";
const LARGE_KEY = "omd-dock-large";

function readFlag(key: string): boolean {
  try {
    return localStorage.getItem(key) === "1";
  } catch {
    return false;
  }
}

function writeFlag(key: string, v: boolean) {
  try {
    localStorage.setItem(key, v ? "1" : "0");
  } catch {
    // preference is not persisted
  }
}

function HeaderBtn({ label, onClick, children }: { label: string; onClick: () => void; children: React.ReactNode }) {
  return (
    <button
      type="button"
      onClick={onClick}
      title={label}
      aria-label={label}
      className="flex h-8 w-8 items-center justify-center rounded-lg text-ink-dim transition-colors hover:bg-fg/[0.08] hover:text-ink"
    >
      {children}
    </button>
  );
}

function DockTile({ cam, width, onRef }: { cam: FloatingCamera; width: string; onRef: (el: HTMLDivElement | null) => void }) {
  const { closeCamera, setMode } = useFloatingCameras();
  return (
    <div ref={onRef} className="flex shrink-0 snap-start flex-col overflow-hidden rounded-xl bg-black ring-1 ring-fg/10" style={{ width }}>
      <div className="flex h-8 items-center gap-1 bg-surface-2 pr-0.5 pl-2.5">
        <span className="min-w-0 flex-1 truncate text-[12px] font-semibold text-ink" title={cam.label}>
          {cam.label}
        </span>
        <button
          type="button"
          onClick={() => setMode(cam.key, "floating")}
          title="Abrir en ventana flotante"
          aria-label={`Abrir ${cam.label} en ventana flotante`}
          className="flex h-7 w-7 items-center justify-center rounded-md text-ink-dim hover:bg-fg/[0.08] hover:text-ink"
        >
          <PopOutIcon size={14} />
        </button>
        <button
          type="button"
          onClick={() => closeCamera(cam.key)}
          title="Quitar de la cinta"
          aria-label={`Quitar ${cam.label} de la cinta`}
          className="flex h-7 w-7 items-center justify-center rounded-md text-ink-dim hover:bg-rose-500/15 hover:text-rose-300"
        >
          <CloseIcon size={14} />
        </button>
      </div>
      <div className="aspect-video">
        <CameraTile
          deviceId={cam.deviceId}
          channel={cam.channel}
          protocol={cam.protocol}
          label={cam.label}
          autoStart={cam.autoStart}
          restartSignal={cam.startNonce}
          bare
        />
      </div>
    </div>
  );
}

export function CameraDock() {
  const { cameras, closeAll } = useFloatingCameras();
  const isMobile = useIsMobile();
  const docked = cameras.filter((c) => c.mode === "dock");
  const [collapsed, setCollapsed] = useState(() => readFlag(COLLAPSED_KEY));
  const [large, setLarge] = useState(() => readFlag(LARGE_KEY));
  const tileRefs = useRef(new Map<string, HTMLDivElement>());
  const knownKeys = useRef(new Set(docked.map((c) => c.key)));
  const balance = useLiveBalance({ deviceId: docked[0]?.deviceId });

  // A new camera in the dock: expand the dock if minimized and scroll the card
  // into view (the dock may hold more cards than fit).
  const keysSignature = docked.map((c) => c.key).join("|");
  useEffect(() => {
    const added = docked.filter((c) => !knownKeys.current.has(c.key));
    knownKeys.current = new Set(docked.map((c) => c.key));
    if (added.length === 0) return;
    if (collapsed) {
      setCollapsed(false);
      writeFlag(COLLAPSED_KEY, false);
    }
    const last = added[added.length - 1];
    requestAnimationFrame(() => tileRefs.current.get(last.key)?.scrollIntoView({ behavior: "smooth", block: "nearest", inline: "nearest" }));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [keysSignature]);

  if (docked.length === 0) return null;

  const width = isMobile ? "min(78vw, 340px)" : large ? "420px" : "288px";

  function toggleCollapsed() {
    setCollapsed((v) => {
      writeFlag(COLLAPSED_KEY, !v);
      return !v;
    });
  }

  function toggleLarge() {
    setLarge((v) => {
      writeFlag(LARGE_KEY, !v);
      return !v;
    });
  }

  return (
    <section aria-label="Cinta de cámaras" className="glass-strong relative z-[500] shrink-0 border-t border-line">
      <div className="flex h-11 items-center gap-2 pr-1.5 pl-3">
        <button type="button" onClick={toggleCollapsed} className="flex min-w-0 items-center gap-2 text-left" aria-expanded={!collapsed}>
          <span className="flex h-7 w-7 shrink-0 items-center justify-center rounded-lg bg-fg/[0.06] text-ink">
            <CameraIcon size={15} />
          </span>
          <span className="text-[13px] font-semibold text-ink">Cámaras</span>
          <span className="rounded-full bg-fg/[0.08] px-1.5 py-0.5 font-data text-[11px] font-semibold text-ink-dim">{docked.length}</span>
        </button>
        <div className="min-w-0 flex-1" />
        {balance && balance.active > 0 && (
          <span
            className="flex min-w-0 items-center gap-1.5 rounded-full bg-rose-500/10 px-2 py-1 text-[11px] font-medium text-rose-200"
            title="Tiempo de video en vivo que le queda a la empresa este mes. Cada cámara en vivo descuenta al mismo tiempo."
          >
            <span className="h-1.5 w-1.5 shrink-0 animate-pulse rounded-full bg-rose-500" aria-hidden />
            <span className="truncate font-data">{formatQuota(balance.remaining)}</span>
            {balance.active > 1 && <span className="shrink-0 text-rose-200/70">· {balance.active} en vivo</span>}
          </span>
        )}
        {!isMobile && !collapsed && (
          <HeaderBtn label={large ? "Tarjetas más chicas" : "Tarjetas más grandes"} onClick={toggleLarge}>
            {large ? <ShrinkIcon /> : <ExpandIcon />}
          </HeaderBtn>
        )}
        <HeaderBtn label={collapsed ? "Mostrar cámaras" : "Minimizar (detiene el video en vivo)"} onClick={toggleCollapsed}>
          {collapsed ? <ChevronUpIcon /> : <ChevronDownIcon />}
        </HeaderBtn>
        <HeaderBtn label="Cerrar todas" onClick={closeAll}>
          <CloseIcon size={15} />
        </HeaderBtn>
      </div>
      {!collapsed && (
        <div
          className="flex snap-x snap-proximity scroll-px-3 gap-3 overflow-x-auto px-3 pb-3"
          onWheel={(e) => {
            // Vertical mouse wheel scrolls the dock horizontally.
            if (Math.abs(e.deltaY) > Math.abs(e.deltaX)) e.currentTarget.scrollLeft += e.deltaY;
          }}
        >
          {docked.map((cam) => (
            <DockTile
              key={cam.key}
              cam={cam}
              width={width}
              onRef={(el) => {
                if (el) tileRefs.current.set(cam.key, el);
                else tileRefs.current.delete(cam.key);
              }}
            />
          ))}
        </div>
      )}
    </section>
  );
}
