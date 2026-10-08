import { useEffect, useRef, useState, type ReactElement } from "react";
import { THEMES, useTheme, type ThemePreference } from "../lib/theme";
import { MonitorIcon, MoonIcon, SparklesIcon, SunIcon } from "./icons";

const ICON: Record<ThemePreference, () => ReactElement> = {
  system: () => <MonitorIcon />,
  dark: () => <MoonIcon />,
  midnight: () => <SparklesIcon />,
  light: () => <SunIcon />,
};

// Color-mode picker for the desktop rail: a single button with the current
// mode's icon that opens a glass menu (instead of 4 stacked buttons eating the
// collapsed rail's footer). `expanded`: the rail is open and there is room for
// the label.
export function ThemeSwitcher({ expanded = false }: { expanded?: boolean }) {
  const { preference, setPreference } = useTheme();
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  const current = THEMES.find((t) => t.id === preference) ?? THEMES[0];
  const CurrentIcon = ICON[current.id];

  useEffect(() => {
    if (!open) return;
    const close = (e: PointerEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    const esc = (e: KeyboardEvent) => e.key === "Escape" && setOpen(false);
    document.addEventListener("pointerdown", close);
    document.addEventListener("keydown", esc);
    return () => {
      document.removeEventListener("pointerdown", close);
      document.removeEventListener("keydown", esc);
    };
  }, [open]);

  return (
    <div ref={ref} className="relative">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        aria-haspopup="menu"
        aria-expanded={open}
        title={`Modo: ${current.label}`}
        className={`flex h-9 w-full items-center gap-2 rounded-xl text-xs text-ink-dim transition-colors hover:bg-fg/[0.06] hover:text-ink ${
          expanded ? "px-3" : "justify-center"
        }`}
      >
        <CurrentIcon />
        {expanded && <span className="font-medium">Modo: {current.label}</span>}
      </button>
      {open && (
        <div role="menu" className="glass-strong absolute bottom-0 left-full z-50 ml-3 w-56 rounded-2xl p-1.5">
          {THEMES.map((t) => {
            const Icon = ICON[t.id];
            const active = preference === t.id;
            return (
              <button
                key={t.id}
                type="button"
                role="menuitemradio"
                aria-checked={active}
                onClick={() => {
                  setPreference(t.id);
                  setOpen(false);
                }}
                className={`flex w-full items-center gap-2.5 rounded-xl px-2.5 py-2 text-left transition-colors ${
                  active ? "bg-brand-600/15 text-ink" : "text-ink-dim hover:bg-fg/[0.06] hover:text-ink"
                }`}
              >
                <Icon />
                <span className="min-w-0 flex-1">
                  <span className="block text-sm font-semibold">{t.label}</span>
                  <span className="block truncate text-[11px] text-ink-faint">{t.description}</span>
                </span>
                {active && <span className="h-2 w-2 rounded-full bg-brand-500" aria-hidden />}
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}

// Labelled version, for the "More" page (mobile).
export function ThemeSwitcherList() {
  const { preference, setPreference } = useTheme();
  return (
    <div role="radiogroup" aria-label="Modo de color" className="grid grid-cols-2 gap-2">
      {THEMES.map((t) => {
        const active = preference === t.id;
        const Icon = ICON[t.id];
        return (
          <button
            key={t.id}
            type="button"
            role="radio"
            aria-checked={active}
            onClick={() => setPreference(t.id)}
            className={`flex items-center gap-2.5 rounded-2xl border px-3 py-2.5 text-left transition-colors ${
              active ? "border-brand-500/60 bg-brand-600/12 text-ink" : "border-line-strong text-ink-dim hover:text-ink"
            }`}
          >
            <Icon />
            <span className="min-w-0">
              <span className="block text-sm font-semibold">{t.label}</span>
              <span className="block truncate text-[11px] text-ink-faint">{t.description}</span>
            </span>
          </button>
        );
      })}
    </div>
  );
}
