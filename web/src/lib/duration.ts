// Viewing-time formatting with second precision: the monthly balance and the
// session countdown are never rounded to minutes, so both customer and provider
// see exactly how much is left.

/** Long balance: "4 h 56 min 12 s", "12 min 05 s", "45 s". */
export function formatQuota(totalSeconds: number): string {
  const s = Math.max(0, Math.floor(totalSeconds));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  const pad = (n: number) => String(n).padStart(2, "0");
  if (h > 0) return `${h} h ${pad(m)} min ${pad(sec)} s`;
  if (m > 0) return `${m} min ${pad(sec)} s`;
  return `${sec} s`;
}

/** Short session countdown: "1:05:09", "4:07", "0:42". */
export function formatCountdown(totalSeconds: number): string {
  const s = Math.max(0, Math.floor(totalSeconds));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = String(s % 60).padStart(2, "0");
  return h > 0 ? `${h}:${String(m).padStart(2, "0")}:${sec}` : `${m}:${sec}`;
}
