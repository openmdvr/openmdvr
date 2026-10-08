// "Today" as the browser's CALENDAR date, never UTC. `new
// Date().toISOString().slice(0, 10)` converts the current instant to UTC before
// truncating; for any tenant west of UTC that means "today" is already TOMORROW
// for a good part of the local afternoon/evening (e.g. UTC-7: from ~17:00 local
// time onwards). A route created in the afternoon with the default "today" date
// would then not show up as the driver's "route today", because each side
// computed its own "today" at a different time of day.
function toLocalIsoDate(d: Date): string {
  const yyyy = d.getFullYear();
  const mm = String(d.getMonth() + 1).padStart(2, "0");
  const dd = String(d.getDate()).padStart(2, "0");
  return `${yyyy}-${mm}-${dd}`;
}

export function todayLocalIso(): string {
  return toLocalIsoDate(new Date());
}

export function daysAgoLocalIso(days: number): string {
  const d = new Date();
  d.setDate(d.getDate() - days);
  return toLocalIsoDate(d);
}

// Converts a "YYYY-MM-DD" date (as produced by an <input type="date">, always in
// the browser's LOCAL timezone) to the real UTC instant of LOCAL midnight of
// that day -- so the backend gets a [from, to) range covering the whole day as
// the user perceives it, regardless of timezone. Unlike the pitfall above
// (deriving the DATE from an instant already converted to UTC), here the instant
// IS built in local time before converting -- the correct use of toISOString().
export function startOfLocalDayIso(dateStr: string): string {
  const [y, m, d] = dateStr.split("-").map(Number);
  return new Date(y, m - 1, d, 0, 0, 0, 0).toISOString();
}

export function startOfNextLocalDayIso(dateStr: string): string {
  const [y, m, d] = dateStr.split("-").map(Number);
  return new Date(y, m - 1, d + 1, 0, 0, 0, 0).toISOString();
}
