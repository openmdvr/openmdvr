import { useEffect, useState } from "react";

// The SINGLE place in the app that decides "mobile or desktop?" -- everything
// else (MapView.tsx, Layout.tsx) consumes it and never duplicates this logic.
// Threshold just below Tailwind's `md` (768px): above it, the desktop shell has
// enough room to work as designed.
const MOBILE_QUERY = "(max-width: 767px)";

export function useIsMobile(): boolean {
  const [isMobile, setIsMobile] = useState(() => window.matchMedia(MOBILE_QUERY).matches);

  useEffect(() => {
    const mql = window.matchMedia(MOBILE_QUERY);
    const onChange = () => setIsMobile(mql.matches);
    mql.addEventListener("change", onChange);
    return () => mql.removeEventListener("change", onChange);
  }, []);

  return isMobile;
}
