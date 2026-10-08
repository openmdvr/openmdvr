import { createContext, useContext, useEffect, useState, type ReactNode } from "react";
import { api, setUnauthorizedHandler } from "./api";
import { closeActivePositionStream } from "./useLivePositions";
import { resetLiveUsage } from "./liveUsage";

interface AuthState {
  token: string | null;
  role: string | null;
  // Lets the UI tell "this row is me" (e.g. hide the deactivate button on one's
  // own account in Users) without decoding the JWT client side -- like
  // tenant_id/role, it comes in LoginResponse (auth.py), never as a claim read
  // in the browser.
  userId: string | null;
  tenantId: string | null;
  isPlatform: boolean;
  // Driver role -- only sees its own shift view (DriverHome.tsx), never the rest
  // of the dashboard. See RequireAuth in App.tsx.
  isDriver: boolean;
  // White label -- null for a platform session or a tenant without a configured
  // brand; Layout.tsx/DriverHome.tsx fall back to the default OpenMDVR logo/name
  // in that case.
  tenantDisplayName: string | null;
  tenantLogoUrl: string | null;
  login: (email: string, password: string) => Promise<void>;
  logout: () => void;
}

const AuthContext = createContext<AuthState | null>(null);

// The JWT lives in localStorage to keep things simple. This is a real trade-off
// (localStorage is readable by any script running on the page, unlike an
// httpOnly cookie) worth revisiting if the dashboard ever renders content we do
// not control (user comments, etc.). Today it does not.
export function AuthProvider({ children }: { children: ReactNode }) {
  const [token, setToken] = useState<string | null>(() => localStorage.getItem("token"));
  const [role, setRole] = useState<string | null>(() => localStorage.getItem("role"));
  const [userId, setUserId] = useState<string | null>(() => localStorage.getItem("userId"));
  const [tenantId, setTenantId] = useState<string | null>(() => localStorage.getItem("tenantId"));
  const [tenantDisplayName, setTenantDisplayName] = useState<string | null>(() => localStorage.getItem("tenantDisplayName"));
  const [tenantLogoUrl, setTenantLogoUrl] = useState<string | null>(() => localStorage.getItem("tenantLogoUrl"));

  const login = async (email: string, password: string) => {
    const resp = await api.login(email, password);
    localStorage.setItem("token", resp.access_token);
    localStorage.setItem("role", resp.role);
    localStorage.setItem("userId", resp.user_id);
    if (resp.tenant_id) localStorage.setItem("tenantId", resp.tenant_id);
    else localStorage.removeItem("tenantId");
    if (resp.tenant_display_name) localStorage.setItem("tenantDisplayName", resp.tenant_display_name);
    else localStorage.removeItem("tenantDisplayName");
    if (resp.tenant_logo_url) localStorage.setItem("tenantLogoUrl", resp.tenant_logo_url);
    else localStorage.removeItem("tenantLogoUrl");
    setToken(resp.access_token);
    setRole(resp.role);
    setUserId(resp.user_id);
    setTenantId(resp.tenant_id);
    setTenantDisplayName(resp.tenant_display_name);
    setTenantLogoUrl(resp.tenant_logo_url);
  };

  const logout = () => {
    // Close the active live-positions EventSource, if any: otherwise a logout
    // might not close the socket, and a subsequent login in the same tab could
    // keep receiving positions from the previous tenant. See
    // web/src/lib/useLivePositions.ts. The notifications stream needs NO
    // explicit close here (unlike positions, which is mounted/unmounted per
    // page): it lives in NotificationsProvider, a global Context whose effect
    // depends on `token` -- as soon as setToken(null) below propagates, React
    // re-runs that effect and its cleanup closes the EventSource. See
    // web/src/lib/useNotifications.tsx.
    closeActivePositionStream();
    resetLiveUsage();
    localStorage.removeItem("token");
    localStorage.removeItem("role");
    localStorage.removeItem("userId");
    localStorage.removeItem("tenantId");
    localStorage.removeItem("tenantDisplayName");
    localStorage.removeItem("tenantLogoUrl");
    setToken(null);
    setRole(null);
    setUserId(null);
    setTenantId(null);
    setTenantDisplayName(null);
    setTenantLogoUrl(null);
  };

  // Any API 401 (expired or revoked token) closes the local session -- without
  // this, RequireAuth (App.tsx) never learns the token stopped being valid and
  // the dashboard stays "logged in" showing empty pages (see the comment in
  // api.ts).
  useEffect(() => {
    setUnauthorizedHandler(logout);
    return () => setUnauthorizedHandler(() => {});
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // localStorage is shared per ORIGIN across tabs, so a login/logout in ANOTHER
  // tab overwrites this tab's token without this AuthProvider noticing -- the
  // old tab would keep showing the PREVIOUS role/tenant while every new request
  // already carried the NEW credentials (api.ts reads the token from
  // localStorage on every call). The `storage` event only fires in the OTHER
  // tabs (never the one that made the change), so there is no need to
  // distinguish login/logout here -- reloading is the simplest way to become
  // consistent, exactly as if the user refreshed manually.
  useEffect(() => {
    function handleStorage(event: StorageEvent) {
      if (event.key === "token" || event.key === "role" || event.key === "tenantId") {
        window.location.reload();
      }
    }
    window.addEventListener("storage", handleStorage);
    return () => window.removeEventListener("storage", handleStorage);
  }, []);

  const isPlatform = role === "super_admin" || role === "support";
  const isDriver = role === "driver";

  return (
    <AuthContext.Provider
      value={{ token, role, userId, tenantId, isPlatform, isDriver, tenantDisplayName, tenantLogoUrl, login, logout }}
    >
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth(): AuthState {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth debe usarse dentro de <AuthProvider>");
  return ctx;
}
