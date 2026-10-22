import { useState, type ReactNode } from "react";
import { NavLink, useLocation } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { ErrorBoundary } from "./ErrorBoundary";
import { useAuth } from "../lib/auth";
import { useNotifications } from "../lib/useNotifications";
import { useIsMobile } from "../lib/useIsMobile";
import { BellIcon, BillingIcon, CameraIcon, GearIcon, GeofenceIcon, LogOutIcon, MapPinIcon, MoreIcon, OperationsIcon, PanelCloseIcon, PanelOpenIcon, ReportIcon, RouteHistoryIcon, TruckIcon } from "./icons";
import { ThemeSwitcher } from "./ThemeSwitcher";
import { LanguageSwitcher } from "./LanguageSwitcher";
import { BottomTabBar, type TabItem } from "./BottomTabBar";
import { CameraDock } from "./CameraDock";

const RAIL_KEY = "omd-rail-expanded";

function readRailExpanded(): boolean {
  try {
    return localStorage.getItem(RAIL_KEY) === "1";
  } catch {
    return false;
  }
}

function Brand({ compact }: { compact: boolean }) {
  const { tenantDisplayName, tenantLogoUrl } = useAuth();
  return (
    <div className={`flex items-center gap-2.5 ${compact ? "justify-center" : ""}`}>
      <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-xl bg-fg/[0.06] ring-1 ring-fg/10">
        <img src={tenantLogoUrl ?? "/logo-mark.png"} alt="" className="h-5 w-5 object-contain" />
      </span>
      {!compact && <span className="truncate text-sm font-semibold tracking-tight text-ink">{tenantDisplayName ?? "OpenMDVR"}</span>}
    </div>
  );
}

function DesktopRail({ expanded, onToggle }: { expanded: boolean; onToggle: () => void }) {
  const { role, logout } = useAuth();
  const { unreadCount } = useNotifications();
  const { t } = useTranslation();

  const navItems = [
    { to: "/map", label: t("nav.map"), Icon: MapPinIcon },
    { to: "/live", label: t("nav.live"), Icon: CameraIcon },
    { to: "/notifications", label: t("nav.notifications"), Icon: BellIcon },
    { to: "/operations", label: t("nav.operations"), Icon: OperationsIcon },
    { to: "/route-history", label: t("nav.history"), Icon: RouteHistoryIcon },
    { to: "/geofences", label: t("nav.geofences"), Icon: GeofenceIcon },
    { to: "/reports", label: t("nav.reports"), Icon: ReportIcon },
    { to: "/billing", label: t("nav.billing"), Icon: BillingIcon, roles: ["super_admin", "support", "tenant_admin"] },
    { to: "/admin", label: t("nav.admin"), Icon: GearIcon },
  ];

  const roleLabel = t(`roles.${role ?? ""}`, { defaultValue: role ?? "U" });

  return (
    <aside className={`glass-rail relative z-20 flex h-full min-h-0 shrink-0 flex-col transition-[width] duration-200 ${expanded ? "w-60" : "w-[72px]"}`}>
      <div className="shrink-0 px-3.5 pt-4 pb-5">
        <Brand compact={!expanded} />
      </div>

      <nav className="no-scrollbar min-h-0 flex-1 space-y-1 overflow-x-hidden overflow-y-auto px-3">
        {navItems
          .filter((item) => !item.roles || item.roles.includes(role ?? ""))
          .map(({ to, label, Icon }) => {
            const badge = to === "/notifications" ? unreadCount : 0;
            return (
              <NavLink
                key={to}
                to={to}
                aria-label={label}
                title={expanded ? undefined : label}
                className={({ isActive }) =>
                  `group/nav relative flex h-11 items-center gap-3 rounded-xl text-sm font-medium transition-colors ${
                    expanded ? "px-3" : "justify-center"
                  } ${isActive ? "bg-brand-600/15 text-brand-400" : "text-ink-dim hover:bg-fg/[0.05] hover:text-ink"}`
                }
              >
                {({ isActive }) => (
                  <>
                    {isActive && <span className="absolute top-2.5 bottom-2.5 -left-3 w-1 rounded-r-full bg-brand-500" aria-hidden />}
                    <span className="relative flex shrink-0">
                      <Icon />
                      {badge > 0 && (
                        <span className="absolute -top-1.5 -right-2 flex h-4 min-w-4 items-center justify-center rounded-full bg-rose-500 px-1 text-[9px] font-bold text-white ring-2 ring-rail">
                          {badge > 9 ? "9+" : badge}
                        </span>
                      )}
                    </span>
                    {expanded && <span className="truncate">{label}</span>}
                  </>
                )}
              </NavLink>
            );
          })}
      </nav>

      <div className="shrink-0 space-y-2 border-t border-line p-3">
        <ThemeSwitcher expanded={expanded} />
        <LanguageSwitcher expanded={expanded} />
        <div className={`flex items-center gap-2.5 rounded-xl py-2 ${expanded ? "px-2" : "justify-center"}`} title={roleLabel}>
          <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-gradient-to-br from-brand-500 to-brand-700 text-xs font-bold text-white">
            {roleLabel.slice(0, 1)}
          </span>
          {expanded && (
            <div className="min-w-0 flex-1">
              <p className="truncate text-xs font-semibold text-ink">{roleLabel}</p>
              <button onClick={logout} className="text-[11px] text-ink-dim hover:text-rose-300">
                {t("layout.logout")}
              </button>
            </div>
          )}
        </div>
        <button
          onClick={onToggle}
          aria-label={expanded ? t("layout.collapse_menu") : t("layout.expand_menu")}
          title={expanded ? t("layout.collapse_menu") : t("layout.expand_menu")}
          className={`flex h-9 w-full items-center gap-2 rounded-xl text-xs text-ink-faint hover:bg-fg/[0.05] hover:text-ink ${expanded ? "px-3" : "justify-center"}`}
        >
          {expanded ? <PanelCloseIcon /> : <PanelOpenIcon />}
          {expanded && t("layout.collapse")}
        </button>
        {!expanded && (
          <button onClick={logout} aria-label={t("layout.logout")} title={t("layout.logout")} className="flex h-9 w-full items-center justify-center rounded-xl text-ink-faint hover:bg-rose-500/10 hover:text-rose-300">
            <LogOutIcon />
          </button>
        )}
      </div>
    </aside>
  );
}

function useMobileTabItems(): TabItem[] {
  const { unreadCount } = useNotifications();
  const { t } = useTranslation();
  return [
    { to: "/map", label: t("nav.map"), Icon: MapPinIcon },
    { to: "/units", label: t("nav.units"), Icon: TruckIcon },
    { to: "/notifications", label: t("nav.alerts"), Icon: BellIcon, badge: unreadCount },
    { to: "/more", label: t("nav.more"), Icon: MoreIcon },
  ];
}

export function Layout({ children }: { children: ReactNode }) {
  const isMobile = useIsMobile();
  const mobileTabItems = useMobileTabItems();
  const [railExpanded, setRailExpanded] = useState(readRailExpanded);
  const { pathname } = useLocation();
  const page = <ErrorBoundary resetKey={pathname}>{children}</ErrorBoundary>;

  function toggleRail() {
    setRailExpanded((v) => {
      try {
        localStorage.setItem(RAIL_KEY, v ? "0" : "1");
      } catch {
        // preference is not persisted
      }
      return !v;
    });
  }

  if (isMobile) {
    return (
      <div className="ambient flex h-full flex-col overflow-hidden">
        <header className="flex shrink-0 items-center justify-between gap-2 px-4 pt-[calc(0.6rem+env(safe-area-inset-top))] pb-2.5">
          <Brand compact={false} />
        </header>
        <main className="min-h-0 flex-1 overflow-y-auto">{page}</main>
        <CameraDock />
        <BottomTabBar items={mobileTabItems} />
      </div>
    );
  }

  return (
    <div className="ambient flex h-full overflow-hidden">
      <DesktopRail expanded={railExpanded} onToggle={toggleRail} />
      <div className="flex min-w-0 flex-1 flex-col">
        <main className="relative min-h-0 flex-1 overflow-y-auto">{page}</main>
        <CameraDock />
      </div>
    </div>
  );
}
