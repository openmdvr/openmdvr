import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    port: 5173,
  },
  // MapLibre GL v6 loads its Web Worker as an ES module (maplibre-gl-worker.mjs
  // imports a shared chunk) -- it is bundled with `?worker&url` in
  // components/MapBaseLayer.tsx; the "es" format allows that code-splitting.
  worker: {
    format: "es",
  },
});
