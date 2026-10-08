// Speedometer with traffic-light speed zones. Shown in DeviceDetailPanel.tsx
// ONLY when the unit has max_speed_kmh configured (see
// 0050_vehicle_max_speed.sql) -- without a limit there are no meaningful
// "zones", so plain text is shown instead. Hand-written SVG, no extra library.
//
// Geometry: classic upper semicircle -- 180° sweep, -90° = left (0 km/h), 0° =
// top, +90° = right (end of the dial). The full dial represents 130% of the
// configured limit (DIAL_HEADROOM), leaving visual room for "how far over the
// limit" instead of pinning the limit to the right end of the arc.
const GAUGE_W = 140;
// The legend text ("km/h · max N") is drawn at y = CENTER_Y + 12 = 90; with a
// viewBox height of 86 its baseline fell outside the viewBox and an <svg> clips
// anything outside it by default (overflow: hidden). GAUGE_H is 96 (10 extra
// units of bottom margin) while CENTER_Y stays the same -- the dial does not
// move, only the canvas below it grows.
const GAUGE_H = 96;
const CENTER_X = GAUGE_W / 2;
const CENTER_Y = 78;
const RADIUS = 56;
const STROKE = 11;
const DIAL_HEADROOM = 1.3;

function polarToCartesian(angleDeg: number, radius: number) {
  const rad = ((angleDeg - 90) * Math.PI) / 180;
  return { x: CENTER_X + radius * Math.cos(rad), y: CENTER_Y + radius * Math.sin(rad) };
}

function describeArc(startAngle: number, endAngle: number, radius: number) {
  const start = polarToCartesian(endAngle, radius);
  const end = polarToCartesian(startAngle, radius);
  const largeArcFlag = endAngle - startAngle <= 180 ? "0" : "1";
  return `M ${start.x} ${start.y} A ${radius} ${radius} 0 ${largeArcFlag} 0 ${end.x} ${end.y}`;
}

function ratioToAngle(ratio: number): number {
  const clamped = Math.min(Math.max(ratio, 0), 1);
  return -90 + clamped * 180;
}

const ZONES: { from: number; to: number; color: string }[] = [
  // Boundaries as fractions of the DIAL (not of the configured limit) -- see
  // DIAL_HEADROOM above. 0.7/1.3 and 1/1.3 place green/amber/red relative to the
  // REAL configured limit, not to the end of the dial.
  { from: 0, to: 0.7 / DIAL_HEADROOM, color: "#10b981" }, // green (emerald-500)
  { from: 0.7 / DIAL_HEADROOM, to: 1 / DIAL_HEADROOM, color: "#daa520" }, // amber (accent-warn, already used in the project)
  { from: 1 / DIAL_HEADROOM, to: 1, color: "#ef4444" }, // red (red-500) -- above the limit
];

// Grey zones when the reading is old (`stale`): a full-color dial reads as "this
// is happening NOW", misleading when it is really the last known position of a
// device that stopped reporting. Same rule as ignition
// (deviceStatus.ts::isDeviceRecent) -- reused here, not a new per-gauge setting.
const STALE_ZONE_COLOR = "#52525b"; // zinc-600, neutral

export function SpeedGauge({
  speedKmh,
  maxSpeedKmh,
  size = 140,
  stale = false,
}: {
  speedKmh: number;
  maxSpeedKmh: number;
  size?: number;
  // true if there is no recent signal from the device (see isDeviceRecent) --
  // the needle/zones desaturate and the text says the data is old instead of
  // showing the number as the current speed.
  stale?: boolean;
}) {
  const dialMax = maxSpeedKmh * DIAL_HEADROOM;
  const speedRatio = dialMax > 0 ? speedKmh / dialMax : 0;
  const needleAngle = ratioToAngle(speedRatio);
  const needleTip = polarToCartesian(needleAngle, RADIUS - STROKE / 2 - 6);
  const overLimit = !stale && speedKmh > maxSpeedKmh;

  return (
    // FLUID width (w-full + max-width instead of a fixed px width): a fixed
    // width clipped the dial in narrow panels. `size` is the MAXIMUM width (it
    // never grows beyond it in a wide panel) but shrinks freely in a narrow one
    // -- viewBox + preserveAspectRatio (SVG default) keep the internal geometry
    // intact at any size.
    <svg
      viewBox={`0 0 ${GAUGE_W} ${GAUGE_H}`}
      className="h-auto w-full"
      style={{ maxWidth: size }}
      role="img"
      aria-label={
        stale
          ? `Última velocidad conocida ${Math.round(speedKmh)} km/h, sin señal reciente del dispositivo`
          : `Velocidad ${Math.round(speedKmh)} de ${maxSpeedKmh} km/h máximo`
      }
    >
      {/*
       * Background track -- the full dial arc, dimmed, so the colored zones
       * read as a "fill" over a base.
       */}
      <path
        d={describeArc(-90, 90, RADIUS)}
        stroke="currentColor"
        className="text-line-strong"
        strokeWidth={STROKE}
        fill="none"
      />
      {ZONES.map((z) => (
        <path
          key={z.color}
          d={describeArc(ratioToAngle(z.from), ratioToAngle(z.to), RADIUS)}
          stroke={stale ? STALE_ZONE_COLOR : z.color}
          strokeWidth={STROKE}
          fill="none"
          opacity="0.85"
        />
      ))}
      <line
        x1={CENTER_X}
        y1={CENTER_Y}
        x2={needleTip.x}
        y2={needleTip.y}
        stroke="currentColor"
        className={stale ? "text-ink-faint" : overLimit ? "text-red-500" : "text-ink"}
        strokeWidth="2.5"
        strokeLinecap="round"
      />
      <circle
        cx={CENTER_X}
        cy={CENTER_Y}
        r="4"
        fill="currentColor"
        className={stale ? "text-ink-faint" : overLimit ? "text-red-500" : "text-ink"}
      />
      {/*
       * fill-ink-dim, not fill-ink-faint -- this is real text to be read (not
       * a decorative icon/line), and ink-faint measures 3.24:1 contrast on the
       * card background (WCAG formula), below the 4.5:1 minimum for normal
       * text.
       */}
      <text
        x={CENTER_X}
        y={CENTER_Y - 4}
        textAnchor="middle"
        className={`font-data ${stale ? "fill-ink-dim" : overLimit ? "fill-red-500" : "fill-ink"}`}
        fontSize="18"
        fontWeight="600"
      >
        {Math.round(speedKmh)}
      </text>
      <text x={CENTER_X} y={CENTER_Y + 13} textAnchor="middle" className="fill-ink-dim" fontSize="10">
        {stale ? "sin señal reciente" : `km/h · máx ${maxSpeedKmh}`}
      </text>
    </svg>
  );
}
