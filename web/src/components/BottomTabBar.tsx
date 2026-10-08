import { NavLink } from "react-router-dom";
import type { ReactNode } from "react";

// Mobile navigation. It is one more element of the flexbox layout (column
// [header][main flex-1][this bar]) -- never position:fixed or an overlay: fixed
// overlays proved unreliable on real phones. It looks like a floating pill
// (margins + radius + glass) but takes its own space in the flow, so it does not
// depend on any stacking trick.
export interface TabItem {
  to: string;
  label: string;
  Icon: (props: { size?: number }) => ReactNode;
  badge?: number;
}

export function BottomTabBar({ items }: { items: TabItem[] }) {
  return (
    <div className="shrink-0 px-3 pt-1.5 pb-[calc(0.5rem+env(safe-area-inset-bottom))]">
      <nav className="glass flex items-stretch justify-around rounded-2xl p-1">
        {items.map(({ to, label, Icon, badge }) => (
          <NavLink
            key={to}
            to={to}
            className={({ isActive }) =>
              `relative flex flex-1 flex-col items-center justify-center gap-0.5 rounded-xl py-1.5 text-[10.5px] font-semibold transition-colors ${
                isActive ? "bg-brand-600/15 text-brand-400" : "text-ink-faint active:bg-fg/5"
              }`
            }
          >
            <span className="relative flex h-6 w-6 items-center justify-center">
              <Icon />
              {badge != null && badge > 0 && (
                <span className="absolute -top-1 -right-2 flex h-4 min-w-4 items-center justify-center rounded-full bg-rose-500 px-1 text-[9px] font-bold text-white ring-2 ring-canvas">
                  {badge > 9 ? "9+" : badge}
                </span>
              )}
            </span>
            {label}
          </NavLink>
        ))}
      </nav>
    </div>
  );
}
