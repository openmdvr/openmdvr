// Shared UI primitives -- avoids repeating the same Tailwind classes in every
// form/page. Deliberately small (no Radix or full component library).
//
// Visual system: glass cards (.glass-card in index.css), generous radii,
// higher-contrast surfaces, statuses as soft pills, buttons with depth and
// visible focus, loading skeletons. Each primitive's API is stable, so pages
// pick up visual changes without modification.
import { forwardRef, type ButtonHTMLAttributes, type InputHTMLAttributes, type LabelHTMLAttributes, type ReactNode, type SelectHTMLAttributes } from "react";
import { ChevronDownIcon } from "./icons";

// forwardRef -- DeviceDetailPanel.tsx ("go to cameras") calls scrollIntoView()
// on a specific Card.
export const Card = forwardRef<HTMLElement, { className?: string; children: ReactNode }>(function Card(
  { className = "", children },
  ref,
) {
  return (
    <section ref={ref} className={`glass-card rounded-2xl p-4 sm:p-5 ${className}`}>
      {children}
    </section>
  );
});

export function CardTitle({ children, action }: { children: ReactNode; action?: ReactNode }) {
  return (
    <div className="mb-4 flex items-center justify-between gap-3">
      <h2 className="text-[15px] font-semibold tracking-tight text-ink">{children}</h2>
      {action}
    </div>
  );
}

const inputClasses =
  "block w-full rounded-xl border border-line-strong bg-fg/[0.04] px-3 py-2 text-sm text-ink outline-none transition-[border-color,box-shadow] placeholder:text-ink-faint focus:border-brand-500 focus:ring-4 focus:ring-brand-600/20 disabled:opacity-50";

export const Input = forwardRef<HTMLInputElement, InputHTMLAttributes<HTMLInputElement>>(function Input(
  { className = "", ...props },
  ref,
) {
  return <input ref={ref} className={`${inputClasses} ${className}`} {...props} />;
});

export const Select = forwardRef<HTMLSelectElement, SelectHTMLAttributes<HTMLSelectElement>>(function Select(
  { className = "", children, ...props },
  ref,
) {
  return (
    <select ref={ref} className={`${inputClasses} ${className}`} {...props}>
      {children}
    </select>
  );
});

export function Field({ label, children }: { label: string; children: ReactNode } & LabelHTMLAttributes<HTMLLabelElement>) {
  return (
    <label className="block text-xs font-medium text-ink-dim">
      {label}
      <div className="mt-1.5">{children}</div>
    </label>
  );
}

type ButtonVariant = "primary" | "secondary" | "ghost";

const buttonVariants: Record<ButtonVariant, string> = {
  primary: "bg-brand-600 text-white shadow-[0_6px_20px_-6px_rgba(3,125,254,0.7)] hover:bg-brand-500 active:bg-brand-700",
  secondary: "border border-line-strong bg-fg/[0.04] text-ink hover:border-fg/20 hover:bg-fg/[0.08]",
  ghost: "bg-transparent text-ink-dim hover:bg-fg/[0.06] hover:text-ink",
};

export function Button({
  variant = "primary",
  className = "",
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & { variant?: ButtonVariant }) {
  return (
    <button
      className={`inline-flex items-center justify-center gap-1.5 rounded-xl px-4 py-2 text-sm font-medium transition-all duration-150 focus-visible:ring-4 focus-visible:ring-brand-600/30 focus-visible:outline-none active:scale-[0.98] disabled:cursor-not-allowed disabled:opacity-50 disabled:active:scale-100 ${buttonVariants[variant]} ${className}`}
      {...props}
    />
  );
}

export function Alert({ variant = "error", children }: { variant?: "error" | "info"; children: ReactNode }) {
  const styles =
    variant === "error" ? "border-rose-500/30 bg-rose-500/10 text-rose-200" : "border-brand-500/30 bg-brand-600/10 text-ink";
  return <p className={`rounded-xl border px-3.5 py-2.5 text-sm ${styles}`}>{children}</p>;
}

// Soft pill (tinted background + dot) -- reads as a status at a glance.
export type BadgeTone = "neutral" | "brand" | "success" | "muted" | "warning" | "danger";

const badgeTone: Record<BadgeTone, { pill: string; dot: string }> = {
  neutral: { pill: "bg-fg/[0.06] text-ink-dim", dot: "bg-slate-400" },
  brand: { pill: "bg-brand-600/15 text-brand-300", dot: "bg-brand-400" },
  success: { pill: "bg-emerald-500/12 text-emerald-300", dot: "bg-emerald-400" },
  muted: { pill: "bg-fg/[0.04] text-ink-faint", dot: "bg-slate-600" },
  warning: { pill: "bg-amber-400/12 text-amber-200", dot: "bg-accent-warn" },
  danger: { pill: "bg-rose-500/14 text-rose-200", dot: "bg-rose-500" },
};

export function Badge({ children, tone = "neutral" }: { children: ReactNode; tone?: BadgeTone }) {
  const t = badgeTone[tone];
  return (
    <span className={`inline-flex items-center gap-1.5 rounded-full px-2 py-0.5 text-[11px] font-semibold whitespace-nowrap ${t.pill}`}>
      <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${t.dot}`} aria-hidden />
      {children}
    </span>
  );
}

export function EmptyState({ children }: { children: ReactNode }) {
  return (
    <div className="flex flex-col items-center gap-2.5 py-10 text-center">
      <span className="flex h-10 w-10 items-center justify-center rounded-2xl bg-fg/[0.04] text-ink-faint" aria-hidden>
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6">
          <circle cx="12" cy="12" r="9" />
          <path d="M8.5 12h7" strokeLinecap="round" />
        </svg>
      </span>
      <div className="max-w-sm text-sm text-ink-dim">{children}</div>
    </div>
  );
}

// Loading block with an animated shimmer (see .skeleton in index.css).
export function Skeleton({ className = "h-4 w-full" }: { className?: string }) {
  return <span className={`skeleton block ${className}`} aria-hidden />;
}

// CSS-only tooltip (:hover/:focus-within, no JS). Grows to the right from the
// trigger (never centered/above: it was clipped against the edge of narrow
// panels), explicit normal-case (it would inherit uppercase from a <th>), and
// scale-0 when closed so it does not add width to an ancestor's overflow.
export function Tooltip({ label, children }: { label: string; children: ReactNode }) {
  return (
    <span className="group/tooltip relative inline-flex items-center focus-within:z-10">
      <span tabIndex={0} className="inline-flex cursor-help items-center outline-none">
        {children}
      </span>
      <span
        role="tooltip"
        className="pointer-events-none absolute top-full left-0 z-20 mt-1.5 w-max max-w-64 origin-top-left scale-0 rounded-lg glass-strong px-2.5 py-1.5 text-xs font-normal tracking-normal text-ink normal-case opacity-0 shadow-lg transition-[opacity,transform] duration-100 group-focus-within/tooltip:scale-100 group-focus-within/tooltip:opacity-100 group-hover/tooltip:scale-100 group-hover/tooltip:opacity-100"
      >
        {label}
      </span>
    </span>
  );
}

// Collapsible section -- native <details>/<summary> (keyboard and focus for
// free). `badge` is visible even when closed (e.g. unread alarms).
export function CollapsibleSection({
  title,
  badge,
  defaultOpen = false,
  children,
}: {
  title: string;
  badge?: ReactNode;
  defaultOpen?: boolean;
  children: ReactNode;
}) {
  return (
    <details className="group glass-card rounded-2xl" open={defaultOpen}>
      <summary className="flex cursor-pointer list-none items-center justify-between gap-2 px-4 py-3 text-sm font-semibold text-ink [&::-webkit-details-marker]:hidden">
        <span className="flex min-w-0 items-center gap-2">
          <span className="truncate">{title}</span>
          {badge}
        </span>
        <span className="shrink-0 text-ink-dim transition-transform duration-150 group-open:rotate-180">
          <ChevronDownIcon size={14} />
        </span>
      </summary>
      <div className="space-y-3 border-t border-line p-4 text-xs text-ink-dim">{children}</div>
    </details>
  );
}

export function Pagination({
  total,
  limit,
  offset,
  onOffsetChange,
}: {
  total: number;
  limit: number;
  offset: number;
  onOffsetChange: (offset: number) => void;
}) {
  if (total === 0) return null;
  const from = offset + 1;
  const to = Math.min(offset + limit, total);
  return (
    <div className="flex items-center justify-between gap-2 border-t border-line pt-3 text-xs text-ink-dim">
      <span>
        {from}–{to} de {total}
      </span>
      <div className="flex gap-1.5">
        <Button variant="secondary" disabled={offset === 0} onClick={() => onOffsetChange(Math.max(0, offset - limit))} className="px-3 py-1.5 text-xs">
          Anterior
        </Button>
        <Button variant="secondary" disabled={to >= total} onClick={() => onOffsetChange(offset + limit)} className="px-3 py-1.5 text-xs">
          Siguiente
        </Button>
      </div>
    </div>
  );
}

// Page container -- mobile-first (on a 375px phone a large fixed padding eats
// the content). `wide` removes the max width.
export function PageContainer({ children, wide = false }: { children: ReactNode; wide?: boolean }) {
  return <div className={`space-y-4 p-4 sm:space-y-6 sm:p-6 lg:p-8 ${wide ? "" : "mx-auto max-w-6xl"}`}>{children}</div>;
}

export function PageHeader({ title, description, action }: { title: string; description?: string; action?: ReactNode }) {
  return (
    <header className="flex flex-wrap items-end justify-between gap-3 sm:gap-4">
      <div className="min-w-0">
        <h1 className="text-xl font-semibold tracking-tight text-ink sm:text-2xl">{title}</h1>
        {description && <p className="mt-1 max-w-2xl text-sm text-ink-dim">{description}</p>}
      </div>
      {action}
    </header>
  );
}
