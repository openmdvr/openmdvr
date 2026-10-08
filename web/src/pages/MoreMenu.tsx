import { NavLink } from "react-router-dom";
import { useAuth } from "../lib/auth";
import { useNotifications } from "../lib/useNotifications";
import { BellIcon, BillingIcon, CameraIcon, GearIcon, GeofenceIcon, OperationsIcon, ReportIcon, RouteHistoryIcon } from "../components/icons";
import { Badge, Button, PageContainer, PageHeader } from "../components/ui";
import { ThemeSwitcherList } from "../components/ThemeSwitcher";

// "More" tab of the mobile bar (Layout.tsx) -- the modules that do not fit as a
// primary tab (Map/Units/Alarms already have one). A regular page, not a
// slide-out menu, for simplicity.
const moreItems = [
  { to: "/live", label: "En vivo", Icon: CameraIcon },
  { to: "/notifications", label: "Notificaciones", Icon: BellIcon },
  { to: "/operations", label: "Operación", Icon: OperationsIcon },
  { to: "/route-history", label: "Historial", Icon: RouteHistoryIcon },
  { to: "/geofences", label: "Geocercas", Icon: GeofenceIcon },
  { to: "/reports", label: "Reportes", Icon: ReportIcon },
  { to: "/billing", label: "Facturación", Icon: BillingIcon, roles: ["super_admin", "support", "tenant_admin"] },
  { to: "/admin", label: "Administración", Icon: GearIcon },
];

export default function MoreMenu() {
  const { role, logout, tenantDisplayName } = useAuth();
  const { unreadCount } = useNotifications();

  return (
    <PageContainer>
      <PageHeader title="Más" description={tenantDisplayName ?? undefined} />
      <div className="glass-card overflow-hidden rounded-2xl">
        <ul className="divide-y divide-line">
          {moreItems
            .filter((item) => !item.roles || item.roles.includes(role ?? ""))
            .map(({ to, label, Icon }) => (
              <li key={to}>
                <NavLink
                  to={to}
                  className="flex items-center justify-between gap-3 px-4 py-3.5 text-sm font-medium text-ink hover:bg-fg/[0.04]"
                >
                  <span className="flex items-center gap-3">
                    <Icon />
                    {label}
                  </span>
                  {to === "/notifications" && unreadCount > 0 && <Badge tone="danger">{unreadCount}</Badge>}
                </NavLink>
              </li>
            ))}
        </ul>
      </div>
      <section className="space-y-2">
        <h2 className="px-1 text-sm font-semibold text-ink">Apariencia</h2>
        <ThemeSwitcherList />
      </section>
      <Button variant="secondary" onClick={logout} className="w-full justify-center">
        Salir ({role})
      </Button>
    </PageContainer>
  );
}
