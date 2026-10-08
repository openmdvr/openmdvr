import { useMemo, useSyncExternalStore } from "react";

// Login illustration: an abstract SVG "map" (generated streets, glowing routes
// and moving units) with floating glass cards on top. Deliberately NOT real
// product screenshots: it weighs nothing, stays sharp at any resolution and
// never exposes data. Always dark on purpose (a showcase that contrasts equally
// well with all three themes). All numbers are illustrative, not from the API.

function mulberry32(seed: number) {
  return () => {
    seed |= 0;
    seed = (seed + 0x6d2b79f5) | 0;
    let t = Math.imul(seed ^ (seed >>> 15), 1 | seed);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

// Streets: gently curved lines on an irregular grid (fixed seed, always the same
// drawing).
function useStreets() {
  return useMemo(() => {
    const rnd = mulberry32(1807);
    const paths: { d: string; w: number }[] = [];
    for (let i = 0; i < 16; i++) {
      const y = 20 + i * 38 + rnd() * 18;
      const c1 = y + (rnd() - 0.5) * 70;
      const c2 = y + (rnd() - 0.5) * 70;
      paths.push({ d: `M -20 ${y} C 260 ${c1}, 540 ${c2}, 820 ${y + (rnd() - 0.5) * 40}`, w: i % 5 === 2 ? 2.2 : 0.9 });
    }
    for (let i = 0; i < 20; i++) {
      const x = 10 + i * 41 + rnd() * 20;
      const c1 = x + (rnd() - 0.5) * 80;
      const c2 = x + (rnd() - 0.5) * 80;
      paths.push({ d: `M ${x} -20 C ${c1} 200, ${c2} 400, ${x + (rnd() - 0.5) * 50} 620`, w: i % 6 === 3 ? 2 : 0.8 });
    }
    return paths;
  }, []);
}

const ROUTES = [
  { id: "r1", d: "M 70 470 C 180 400, 230 300, 350 290 S 520 330, 600 220 S 700 120, 760 90", color: "#2f93ff", dur: 16 },
  { id: "r2", d: "M 40 170 C 150 210, 250 160, 330 210 S 470 390, 590 410 S 720 470, 790 520", color: "#22c55e", dur: 20 },
  { id: "r3", d: "M 250 580 C 280 470, 390 450, 430 360 S 420 190, 520 120 S 650 60, 700 20", color: "#a78bfa", dur: 18 },
];

function subscribeReducedMotion(cb: () => void) {
  const mq = window.matchMedia("(prefers-reduced-motion: reduce)");
  mq.addEventListener("change", cb);
  return () => mq.removeEventListener("change", cb);
}

function Chip({ className, children }: { className: string; children: React.ReactNode }) {
  return (
    <div
      className={`absolute rounded-2xl border border-white/12 bg-[rgba(18,22,30,0.62)] px-4 py-3 text-white shadow-[0_20px_50px_-20px_rgba(0,0,0,0.8)] backdrop-blur-xl ${className}`}
      style={{ boxShadow: "inset 0 1px 0 rgba(255,255,255,0.14), 0 24px 60px -24px rgba(0,0,0,0.85)" }}
    >
      {children}
    </div>
  );
}

export function LoginHero() {
  const streets = useStreets();
  const reducedMotion = useSyncExternalStore(subscribeReducedMotion, () => window.matchMedia("(prefers-reduced-motion: reduce)").matches);

  return (
    <div className="relative h-full w-full overflow-hidden rounded-[28px] bg-[#070b12] ring-1 ring-white/10">
      <svg viewBox="0 0 800 600" preserveAspectRatio="xMidYMid slice" className="absolute inset-0 h-full w-full" aria-hidden>
        <defs>
          <radialGradient id="lh-glow" cx="30%" cy="25%" r="75%">
            <stop offset="0%" stopColor="#0b2a55" stopOpacity="0.9" />
            <stop offset="60%" stopColor="#070b12" stopOpacity="0" />
          </radialGradient>
          <radialGradient id="lh-glow2" cx="85%" cy="90%" r="55%">
            <stop offset="0%" stopColor="#2a1459" stopOpacity="0.55" />
            <stop offset="100%" stopColor="#070b12" stopOpacity="0" />
          </radialGradient>
          <filter id="lh-blur" x="-20%" y="-20%" width="140%" height="140%">
            <feGaussianBlur stdDeviation="6" />
          </filter>
        </defs>
        <rect width="800" height="600" fill="url(#lh-glow)" />
        <rect width="800" height="600" fill="url(#lh-glow2)" />
        {streets.map((s, i) => (
          <path key={i} d={s.d} fill="none" stroke="#9fb4d8" strokeOpacity={s.w > 1 ? 0.16 : 0.07} strokeWidth={s.w} />
        ))}
        {ROUTES.map((r) => (
          <g key={r.id}>
            <path d={r.d} fill="none" stroke={r.color} strokeWidth="9" strokeOpacity="0.35" filter="url(#lh-blur)" />
            <path id={r.id} d={r.d} fill="none" stroke={r.color} strokeWidth="2.6" strokeLinecap="round" />
            <circle r="7" fill={r.color} stroke="white" strokeWidth="2.5">
              {!reducedMotion && <animateMotion dur={`${r.dur}s`} repeatCount="indefinite" rotate="auto" keyPoints="0;1" keyTimes="0;1" calcMode="linear">
                <mpath href={`#${r.id}`} />
              </animateMotion>}
            </circle>
          </g>
        ))}
        {/* Illustrative geofence */}
        <circle cx="590" cy="410" r="46" fill="#22c55e" fillOpacity="0.08" stroke="#22c55e" strokeOpacity="0.55" strokeDasharray="5 6" />
      </svg>

      <div className="pointer-events-none absolute inset-0 bg-gradient-to-t from-[#070b12] via-transparent to-transparent" />

      <Chip className="top-8 left-8">
        <div className="flex items-center gap-2 text-[11px] font-semibold tracking-wide text-white/70">
          <span className="relative flex h-2 w-2">
            <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-emerald-400 opacity-60 motion-reduce:hidden" />
            <span className="relative inline-flex h-2 w-2 rounded-full bg-emerald-400" />
          </span>
          FLOTA EN VIVO
        </div>
        <p className="mt-1 text-2xl font-semibold tracking-tight">
          128 <span className="text-sm font-medium text-white/60">unidades</span>
        </p>
        <div className="mt-2 flex gap-3 text-[11px] text-white/70">
          <span><b className="text-[#6ee7b7]">94</b> en ruta</span>
          <span><b className="text-[#fcd34d]">21</b> ralentí</span>
          <span><b className="text-[#fda4af]">3</b> alertas</span>
        </div>
      </Chip>

      <Chip className="top-[40%] right-8 w-64 [@media(max-height:560px)]:hidden">
        <p className="text-[11px] font-semibold tracking-wide text-white/60">GEOCERCA</p>
        <p className="mt-1 text-sm font-semibold">Entrada a Centro de distribución</p>
        <p className="mt-0.5 text-xs text-white/60">Unidad T-104 · hace 12 s</p>
      </Chip>

      <Chip className="top-8 right-8 w-56">
        <p className="text-[11px] font-semibold tracking-wide text-white/60">KILÓMETROS HOY</p>
        <p className="mt-1 text-xl font-semibold tracking-tight">18,420 km</p>
        <svg viewBox="0 0 200 40" className="mt-2 h-8 w-full" aria-hidden>
          <path d="M0 32 C 20 30, 30 20, 50 22 S 80 10, 100 14 S 140 26, 160 12 S 185 6, 200 4" fill="none" stroke="#2f93ff" strokeWidth="2.5" strokeLinecap="round" />
          <path d="M0 32 C 20 30, 30 20, 50 22 S 80 10, 100 14 S 140 26, 160 12 S 185 6, 200 4 L200 40 L0 40Z" fill="#2f93ff" fillOpacity="0.15" />
        </svg>
      </Chip>

      <div className="absolute inset-x-10 bottom-10 text-white">
        <h2 className="text-4xl leading-[1.05] font-semibold tracking-tight xl:text-5xl">
          Tu flota,
          <br />
          <span className="bg-gradient-to-r from-[#5aa9ff] via-[#8cc3ff] to-[#a78bfa] bg-clip-text text-transparent">en vivo.</span>
        </h2>
        <p className="mt-3 max-w-md text-sm text-white/65">Video, GPS, geocercas y alertas en una sola plataforma segura.</p>
      </div>
    </div>
  );
}
