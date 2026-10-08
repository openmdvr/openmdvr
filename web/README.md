# web

OpenMDVR dashboard: React 18 + Vite + TypeScript + Tailwind CSS v4.

A fleet monitoring UI for dashcams (JT808/JT1078) and GPS trackers (GT06):
live map, multi-camera live video, alarms/notifications, route history,
geofences, reports, operations (driver shifts and routes), billing, and
multi-tenant administration. A separate mobile-first view serves drivers.

> The user-facing UI text is currently in Spanish. Internationalization is
> planned; code and comments are in English.

## Running locally

```bash
cd web
npm ci
cp .env.example .env   # VITE_API_BASE_URL, default http://127.0.0.1:8000
npm run dev            # http://localhost:5173
```

The dashboard needs the API running and reachable at `VITE_API_BASE_URL`
(see `infra/`, e.g. `docker compose up -d api`). To see real video you also
need `jt808-server` + `zlmediakit` and a device with an active session;
`jt808-server/testclient/simulate_video.py` lets you test without hardware.

Build for production:

```bash
npm run build          # tsc -b && vite build -> dist/
```

The `Dockerfile` builds the app and serves `dist/` with nginx (SPA fallback
to `index.html`, defense-in-depth headers). `VITE_API_BASE_URL` is a build
argument: it is baked into the bundle, so changing it requires a rebuild.

### Environment variables

| Variable | Purpose |
| --- | --- |
| `VITE_API_BASE_URL` | Base URL of the FastAPI backend. |
| `VITE_CARTO_API_KEY` | Optional. Enables CARTO as a raster map fallback. Public by nature (domain-restrict it in the CARTO dashboard). |

## Structure

```
src/
  App.tsx            routes, auth/role gates, lazy-loaded pages
  main.tsx           entry point, applies the saved theme
  index.css          design tokens, themes, glass material, third-party skins
  components/        shared UI (ui.tsx primitives, icons.tsx, Layout, CameraTile, map layers...)
  lib/               API client, auth, live data hooks, formatting helpers
  pages/             one file per route
  types/             type shims for untyped dependencies (leaflet-hotline)
public/              logo-mark.svg, favicon.svg
```

Key pages:

- `MapView.tsx` — full-screen live map with floating unit list/detail panels,
  status-colored markers, clustering, fleet summary filters.
- `LiveView.tsx` / `DeviceVideo.tsx` — multi-camera and single-camera live video.
- `RouteHistory.tsx` — speed-colored route (leaflet-hotline), stats, timeline playback.
- `Geofences.tsx` — draw circles/polygons by tap, occupancy and events.
- `Notifications.tsx` — in-app mailbox (alarms), on-demand alarm video clips.
- `Reports.tsx` — distance, driver hours, engine hours, geofence visits (CSV export).
- `Operations.tsx` — who is on shift, routes, policy alerts.
- `Dashboard.tsx` / `TenantWorkspace.tsx` — platform tenant list and per-tenant workspace (users, vehicles, drivers, devices, groups, webhooks, API keys, branding).
- `Billing.tsx` — platform billing (catalog, subscriptions, invoices, payments, profitability, SIM usage) and "my billing" for tenant admins.
- `DriverHome.tsx` — mobile-first driver view (clock in/out, meal, today's route).

## Key design decisions

### API access and auth

- `lib/api.ts` is a thin `fetch()` client (no axios / data-fetching framework).
  Every call goes through one choke point that adds the JWT, normalizes
  errors (`ApiError`) and applies a request timeout (native `fetch()` never
  times out on its own).
- The JWT is stored in `localStorage` (simple; revisit if the dashboard ever
  renders untrusted content). A global 401 handler logs the session out, so
  an expired token never leaves the UI "logged in" with empty pages. A
  `storage` event listener reloads the tab when another tab logs in/out.
- The UI never decodes the JWT; role/tenant/branding come from the login
  response.
- **Role gates in the UI are UX, never security.** The backend (role
  dependencies + Postgres RLS) is the real barrier. The UI still never shows
  a control the backend would reject (`canManage`, `AdminRoot`,
  `RequireDriver`), so permissions are not misrepresented on screen.
- Login error messages are deliberately generic (one message for invalid
  credentials, one for anything else) and never distinguish field or cause.

### Live video

- Video is **on demand**: nothing is requested until the user clicks
  "watch live". For cameras that support it, a cheap **preview photo** is
  shown first, refreshed at most a few times while the tile is visible.
- `components/CameraTile.tsx` plays live video over **WebRTC (WHEP)** using a
  hand-written client (`RTCPeerConnection` + a few HTTP calls), skinned with
  Plyr. WebRTC is native on Safari/iOS, unlike MSE-based HTTP-FLV.
- The video URL carries a **one-time ticket** minted by the API after
  checking that the device belongs to the caller's tenant; ZLMediaKit
  validates it in its `on_play` hook. Every retry requests a new ticket.
- Per-session time limits and the monthly quota are **enforced server side**
  by the bridge; the UI only shows a countdown and the shared balance
  (`lib/liveUsage.ts`). Retries only happen for transient failures
  (502/503/network), never for 402/404/400.
- Recorded alarm clips (`AlarmClipPlayer.tsx`) are MPEG-TS files played with
  mpegts.js from short-lived signed storage URLs.
- Cameras open in a **dock** below the content or in **floating windows**
  that survive navigation and can be popped out with Document
  Picture-in-Picture (`lib/floatingCameras.tsx`, `components/FloatingCameras.tsx`).

### Real-time data

- GPS positions and ignition/power state are **pushed over SSE**
  (`lib/useLivePositions.ts`); notifications use the same pattern in a single
  global provider (`lib/useNotifications.tsx`).
- `EventSource` cannot send an `Authorization` header, so each stream uses a
  **one-time ticket**. Because of that the native `EventSource` retry is never
  used: the hooks implement their own reconnect with exponential backoff.
- A low-frequency REST reconciliation runs alongside the push as a safety
  net, and the positions stream is closed explicitly on logout.

### Maps

- Leaflet via `react-leaflet` (v4, React 18). `components/MapBaseLayer.tsx` is
  the single base layer for all maps:
  1. a raster provider forced by a super admin (platform setting), else
  2. the user's chosen style (`lib/mapStyle.ts`) — OpenFreeMap vector styles
     rendered with MapLibre GL inside Leaflet (loaded lazily), else
  3. automatic raster failover (OpenStreetMap → Esri → CARTO if a key is set),
     driven by tile error rates in each browser (`lib/mapProviders.ts`).
- Attributions for OpenStreetMap, OpenFreeMap, Esri and CARTO are required by
  their terms and must be kept.
- Markers, clusters (`leaflet.markercluster`), geofences and trails are
  regular react-leaflet layers on top.

### Layout and styling

- **Mobile-first.** `lib/useIsMobile.ts` is the only place that decides
  mobile vs. desktop. Mobile uses a bottom tab bar + "More" page.
- **Never `position: fixed`** for navigation or primary overlays: everything
  lives in normal flow or is `absolute` inside a relative container. Each
  Leaflet container gets its own stacking context (`isolation: isolate`).
- Tailwind v4 with design tokens as CSS variables. Themes (dark, midnight,
  light, system) are just `[data-theme]` blocks overriding variables
  (`index.css` + `lib/theme.ts`); an inline script in `index.html` applies the
  theme before first paint.
- `components/ui.tsx` holds small in-house primitives (no component library);
  `components/icons.tsx` is the single entry point for icons (lucide-react).
- Pages are code-split with `React.lazy`; each page has its own error boundary.

### Lists and scale

- Admin tables use the API's `Page[T]` envelope (`items/total/limit/offset`,
  server-side search) with small fixed page sizes.
- Map/live/units views request the full roster with `limit: 1000` — a
  pragmatic ceiling, not a real scaling solution for very large fleets.

### Dates

- "Today" is always the browser's local calendar date (`lib/localDate.ts`),
  never derived from a UTC ISO string; date filters are converted to the UTC
  instants of local midnight before reaching the API.

### CSV export

- Reports are generated client side as CSV with a UTF-8 BOM, and cells that
  could be interpreted as formulas are neutralized (OWASP CSV injection).

## Dependencies

Versions of security-relevant dependencies are pinned exactly in
`package.json`. Third-party code that processes untrusted input or is
loaded at runtime (map engines, players, plugins) was reviewed before
adoption.

`npm audit` reports advisories in `react-router` 6.x that do not apply to
this app: SSR hydration (this is a static SPA) and open redirects from
user-controlled navigation targets (all `<Link>`/`navigate()` targets are
literal routes or IDs from our own API). Re-evaluate if navigation to
externally supplied URLs is ever added.
