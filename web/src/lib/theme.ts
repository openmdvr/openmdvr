import { useSyncExternalStore } from "react";

// Interface color mode. Colors live in index.css as variables per [data-theme];
// this module only decides WHICH theme to apply and writes it to <html
// data-theme="…">. To add a new mode: 1. a [data-theme="new"] block in index.css
// (variables only); 2. an entry in THEMES here. The preference is per browser
// (localStorage, a viewer convenience wrapped in try/catch). "system" follows
// prefers-color-scheme live. index.html repeats the initial resolution in an
// inline script so the first paint already has the right theme (no flash).

export type ThemePreference = "system" | "dark" | "midnight" | "light";
export type ResolvedTheme = Exclude<ThemePreference, "system">;

export const THEMES: { id: ThemePreference; label: string; description: string }[] = [
  { id: "system", label: "Sistema", description: "Sigue el modo de tu dispositivo" },
  { id: "dark", label: "Oscuro", description: "Grafito neutro" },
  { id: "midnight", label: "Medianoche", description: "Negro profundo, ideal OLED" },
  { id: "light", label: "Claro", description: "Superficies blancas" },
];

const STORAGE_KEY = "omd-theme";
const THEME_COLOR: Record<ResolvedTheme, string> = { dark: "#090a0c", midnight: "#000000", light: "#eef1f6" };
const listeners = new Set<() => void>();

function readPreference(): ThemePreference {
  try {
    const v = localStorage.getItem(STORAGE_KEY) as ThemePreference | null;
    return v && THEMES.some((t) => t.id === v) ? v : "system";
  } catch {
    return "system";
  }
}

function resolve(pref: ThemePreference): ResolvedTheme {
  if (pref !== "system") return pref;
  return typeof window !== "undefined" && window.matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark";
}

let preference: ThemePreference = readPreference();

function apply() {
  const theme = resolve(preference);
  const root = document.documentElement;
  root.dataset.theme = theme;
  // Mobile browser bar in the same color as the app.
  let meta = document.querySelector<HTMLMetaElement>('meta[name="theme-color"]');
  if (!meta) {
    meta = document.createElement("meta");
    meta.name = "theme-color";
    document.head.appendChild(meta);
  }
  meta.content = THEME_COLOR[theme];
  listeners.forEach((l) => l());
}

if (typeof window !== "undefined") {
  apply();
  window.matchMedia("(prefers-color-scheme: light)").addEventListener("change", () => {
    if (preference === "system") apply();
  });
}

export function setThemePreference(pref: ThemePreference) {
  preference = pref;
  try {
    localStorage.setItem(STORAGE_KEY, pref);
  } catch {
    // not persisted; the theme stays applied until reload
  }
  apply();
}

function subscribe(cb: () => void) {
  listeners.add(cb);
  return () => listeners.delete(cb);
}

export function useTheme(): { preference: ThemePreference; resolved: ResolvedTheme; setPreference: (p: ThemePreference) => void } {
  const pref = useSyncExternalStore(subscribe, () => preference);
  const resolved = useSyncExternalStore(subscribe, () => resolve(preference));
  return { preference: pref, resolved, setPreference: setThemePreference };
}
