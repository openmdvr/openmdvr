import { lazy, Suspense } from "react";
import { Navigate, Outlet, Route, Routes } from "react-router-dom";
import { Skeleton } from "./components/ui";
import { AuthProvider, useAuth } from "./lib/auth";
import { NotificationsProvider } from "./lib/useNotifications";
import { FloatingCamerasProvider } from "./lib/floatingCameras";
import { FloatingCameraLayer } from "./components/FloatingCameras";
import { Layout } from "./components/Layout";
import Login from "./pages/Login";

// Code-split per page (React.lazy): each section downloads its own chunk on
// first visit, so the login screen only loads what it needs.
const LiveView = lazy(() => import("./pages/LiveView"));
const MapView = lazy(() => import("./pages/MapView"));
const Notifications = lazy(() => import("./pages/Notifications"));
const Dashboard = lazy(() => import("./pages/Dashboard"));
const Operations = lazy(() => import("./pages/Operations"));
const Reports = lazy(() => import("./pages/Reports"));
const RouteHistory = lazy(() => import("./pages/RouteHistory"));
const Geofences = lazy(() => import("./pages/Geofences"));
const Billing = lazy(() => import("./pages/Billing"));
const DriverHome = lazy(() => import("./pages/DriverHome"));
const DeviceVideo = lazy(() => import("./pages/DeviceVideo"));
const DeviceCommandHistory = lazy(() => import("./pages/DeviceCommandHistory"));
const TenantWorkspace = lazy(() => import("./pages/TenantWorkspace"));
const DeviceConfigCommands = lazy(() => import("./pages/DeviceConfigCommands"));
const UnitsView = lazy(() => import("./pages/UnitsView"));
const UnitDetail = lazy(() => import("./pages/UnitDetail"));
const MoreMenu = lazy(() => import("./pages/MoreMenu"));

// Regular dashboard routes (rail, map, admin, etc.). A driver must never land
// here: the account has none of the privileges these pages assume (see
// require_tenant_admin/require_bypass in api/app/deps.py; RLS is the real
// guarantee, this only avoids showing a driver a broken screen).
//
// Layout route (with <Outlet/>) instead of wrapping each route: the shell (rail,
// mobile bar, their state) mounts once per session instead of on every
// navigation.
// While a page chunk downloads: a quiet skeleton, not a spinner.
function PageFallback() {
  return (
    <div className="space-y-4 p-6">
      <Skeleton className="h-8 w-56" />
      <Skeleton className="h-4 w-80" />
      <Skeleton className="h-64 w-full rounded-2xl" />
    </div>
  );
}

function AuthedShell() {
  const { token, isDriver } = useAuth();
  if (!token) return <Navigate to="/login" replace />;
  if (isDriver) return <Navigate to="/driver" replace />;
  return (
    <Layout>
      <Suspense fallback={<PageFallback />}>
        <Outlet />
      </Suspense>
    </Layout>
  );
}

// The driver view is its own mobile-first page without the desktop rail
// (DriverHome.tsx has its own header and logout button); a driver has no other
// screen to navigate to.
function RequireDriver({ children }: { children: React.ReactNode }) {
  const { token, isDriver } = useAuth();
  if (!token) return <Navigate to="/login" replace />;
  if (!isDriver) return <Navigate to="/map" replace />;
  return <>{children}</>;
}

// /admin: a tenant_admin session manages a single tenant (its own) and lands
// directly on its workspace (TenantWorkspace pinned to its tenantId, no tenant
// list). A platform session sees the full list (Dashboard) and opens one
// tenant's workspace from there (/admin/tenants/:tenantId).
//
// tenant_operator/tenant_viewer never administer anything, not even their own
// tenant. Treating "any role that is not tenant_admin" as platform would show
// them the platform Dashboard (tenant table, plan fields) even though the
// backend rejects every write with 403 -- a misrepresentation of permissions.
// Same rule as RequireDriver: a role with nothing to manage here does not even
// see the UI and is sent to /map.
function AdminRoot() {
  const { role, isPlatform } = useAuth();
  if (role === "tenant_admin") return <TenantWorkspace />;
  if (isPlatform) return <Dashboard />;
  return <Navigate to="/map" replace />;
}

// GT06 configuration commands: platform only (super_admin/support); not even
// tenant_admin gets here, unlike the rest of /admin/*. Same rule as
// RequireDriver/AdminRoot: a role with nothing to do on this screen does not see
// the UI.
function RequirePlatform({ children }: { children: React.ReactNode }) {
  const { isPlatform } = useAuth();
  if (!isPlatform) return <Navigate to="/admin" replace />;
  return <>{children}</>;
}

export default function App() {
  return (
    <AuthProvider>
      {/*
       * Inside AuthProvider on purpose: NotificationsProvider reads
       * token/isDriver from useAuth(). A single global Provider lets the rail
       * badge and /notifications share the same state.
       */}
      <NotificationsProvider>
        {/*
         * Floating cameras: above <Routes> so windows survive page navigation.
         * The layer draws inside this relative container (absolute inset-0),
         * never position:fixed.
         */}
        <FloatingCamerasProvider>
          <div className="relative h-full">
            <Routes>
              <Route path="/login" element={<Login />} />
              <Route path="/" element={<Navigate to="/map" replace />} />
              <Route
                path="/driver"
                element={
                  <RequireDriver>
                    <Suspense fallback={<PageFallback />}>
                      <DriverHome />
                    </Suspense>
                  </RequireDriver>
                }
              />
              <Route element={<AuthedShell />}>
                <Route path="/live" element={<LiveView />} />
                <Route path="/map" element={<MapView />} />
                <Route path="/notifications" element={<Notifications />} />
                <Route path="/units" element={<UnitsView />} />
                <Route path="/units/:deviceId" element={<UnitDetail />} />
                <Route path="/more" element={<MoreMenu />} />
                <Route path="/operations" element={<Operations />} />
                <Route path="/admin" element={<AdminRoot />} />
                <Route path="/admin/tenants/:tenantId" element={<TenantWorkspace />} />
                <Route
                  path="/admin/devices/:deviceId/config-commands"
                  element={
                    <RequirePlatform>
                      <DeviceConfigCommands />
                    </RequirePlatform>
                  }
                />
                <Route path="/reports" element={<Reports />} />
                <Route path="/geofences" element={<Geofences />} />
                <Route path="/route-history" element={<RouteHistory />} />
                <Route path="/billing" element={<Billing />} />
                <Route path="/devices/:deviceId" element={<DeviceVideo />} />
                <Route path="/devices/:deviceId/commands" element={<DeviceCommandHistory />} />
              </Route>
              <Route path="*" element={<Navigate to="/map" replace />} />
            </Routes>
            <FloatingCameraLayer />
          </div>
        </FloatingCamerasProvider>
      </NotificationsProvider>
    </AuthProvider>
  );
}
