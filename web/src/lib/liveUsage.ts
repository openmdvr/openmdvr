import { useSyncExternalStore } from "react";
import { api } from "./api";

// Live video balance SHARED by all open cameras.
//
// There is a single source per tenant: the server's central meter (GET
// /devices/{id}/live-view-balance) reports how much is left and how many cameras
// are open across the WHOLE tenant (including other users'), and between queries
// the balance is decremented locally at that rate (2 cameras = 2 s per second).
// Per-tile counters would each show the balance dropping as if they were the
// only camera.
//
// Efficient: one query per tenant every POLL_MS while at least one camera is
// playing, regardless of how many. No queries when nothing is playing.

const POLL_MS = 10_000;

interface Balance {
  remaining: number; // seconds, at syncedAt
  active: number; // cameras open in the tenant
  syncedAt: number;
}

const players = new Map<string, string>(); // playerId -> deviceId
const deviceTenant = new Map<string, string>(); // deviceId -> tenantId
const balances = new Map<string, Balance>(); // tenantId -> balance
const listeners = new Set<() => void>();
let version = 0;
let timer: ReturnType<typeof setInterval> | null = null;
// A single 1 s tick for all consumers (never one per camera).
let ticker: ReturnType<typeof setInterval> | null = null;

function emit() {
  version++;
  listeners.forEach((l) => l());
}

async function refresh() {
  // One query per tenant: any device of that tenant will do.
  const byTenant = new Map<string, string>();
  const unknown = new Set<string>();
  for (const deviceId of players.values()) {
    const tenant = deviceTenant.get(deviceId);
    if (tenant) byTenant.set(tenant, deviceId);
    else unknown.add(deviceId);
  }
  const targets = [...byTenant.values(), ...unknown];
  await Promise.all(
    targets.map(async (deviceId) => {
      try {
        const b = await api.liveViewBalance(deviceId);
        deviceTenant.set(deviceId, b.tenant_id);
        balances.set(b.tenant_id, { remaining: b.remaining_seconds, active: b.active_sessions, syncedAt: Date.now() });
      } catch {
        // no new balance: keep decrementing the last known one
      }
    }),
  );
  emit();
}

function ensureTimer() {
  if (players.size > 0 && !timer) {
    timer = setInterval(refresh, POLL_MS);
    ticker = setInterval(emit, 1000);
  }
  if (players.size === 0 && timer) {
    clearInterval(timer);
    timer = null;
    if (ticker) clearInterval(ticker);
    ticker = null;
  }
}

/** A camera started playing: the server has already opened its session. */
export function registerLivePlayer(playerId: string, deviceId: string) {
  players.set(playerId, deviceId);
  ensureTimer();
  void refresh();
}

export function unregisterLivePlayer(playerId: string) {
  const deviceId = players.get(playerId);
  if (!players.delete(playerId)) return;
  ensureTimer();
  // The session closed: the decrement rate changes now, without waiting for the
  // next cycle.
  const tenant = deviceId ? deviceTenant.get(deviceId) : undefined;
  if (tenant) {
    const b = balances.get(tenant);
    if (b) balances.set(tenant, { remaining: currentRemaining(b), active: Math.max(0, b.active - 1), syncedAt: Date.now() });
  }
  if (players.size > 0) void refresh();
  emit();
}

function currentRemaining(b: Balance): number {
  return Math.max(0, Math.round(b.remaining - (b.active * (Date.now() - b.syncedAt)) / 1000));
}

function subscribe(l: () => void) {
  listeners.add(l);
  return () => listeners.delete(l);
}

export interface LiveBalanceView {
  remaining: number;
  active: number;
}

/**
 * Current (already decremented) balance of `deviceId`'s tenant, or of the
 * given tenant. Re-renders every second while cameras are open in that tenant.
 * null if not known yet.
 */
export function useLiveBalance(opts: { deviceId?: string; tenantId?: string | null }): LiveBalanceView | null {
  useSyncExternalStore(subscribe, () => version);
  const tenant = opts.tenantId ?? (opts.deviceId ? deviceTenant.get(opts.deviceId) : undefined);
  const b = tenant ? balances.get(tenant) : undefined;
  if (!b) return null;
  // A balance with no open cameras goes stale: after 60 s without queries the
  // local value may be outdated (another user may have consumed some).
  if (b.active === 0 && Date.now() - b.syncedAt > 60_000) return null;
  return { remaining: currentRemaining(b), active: b.active };
}

/** Clears everything (logout). */
export function resetLiveUsage() {
  players.clear();
  deviceTenant.clear();
  balances.clear();
  ensureTimer();
  emit();
}
