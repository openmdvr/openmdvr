import { NavLink } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { useAuth } from "../lib/auth";
import { useNotifications } from "../lib/useNotifications";
import { BellIcon, BillingIcon, CameraIcon, GearIcon, GeofenceIcon, OperationsIcon, ReportIcon, RouteHistoryIcon } from "../components/icons";
import { Badge, Button, PageContainer, PageHeader } from "../components/ui";
import { ThemeSwitcherList } from "../components/ThemeSwitcher";
import { LanguageSwitcherListItem } from "../components/LanguageSwitcher";

export default function MoreMenu() {
  const { role, logout, tenantDisplayName } = useAuth();
  const { unreadCount } = useNotifications();
  const { t } = useTranslation();

  const moreItems = [
    { to: "/live", label: t("nav.live"), Icon: CameraIcon },
    { to: "/notifications", label: t("nav.notifications"), Icon: BellIcon },
    { to: "/operations", label: t("nav.operations"), Icon: OperationsIcon },
    { to: "/route-history", label: t("nav.history"), Icon: RouteHistoryIcon },
    { to: "/geofences", label: t("nav.geofences"), Icon: GeofenceIcon },
    { to: "/reports", label: t("nav.reports"), Icon: ReportIcon },
    { to: "/billing", label: t("nav.billing"), Icon: BillingIcon, roles: ["super_admin", "support", "tenant_admin"] },
    { to: "/admin", label: t("nav.admin"), Icon: GearIcon },
  ];

  return (
    <PageContainer>
      <PageHeader title={t("more_menu.title")} description={tenantDisplayName ?? undefined} />
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
        <h2 className="px-1 text-sm font-semibold text-ink">{t("more_menu.appearance")}</h2>
        <ThemeSwitcherList />
        <LanguageSwitcherListItem />
      </section>
      <Button variant="secondary" onClick={logout} className="w-full justify-center">
        {t("more_menu.sign_out", { role: t(`roles.${role ?? ""}`, { defaultValue: role ?? "" }) })}
      </Button>
    </PageContainer>
  );
}
