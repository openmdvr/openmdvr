import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api, ApiError, type AlarmSeverity, type DeviceHealthEvent } from "../lib/api";
import { useAuth } from "../lib/auth";
import { Alert, Card } from "./ui";

// Device health (platform only, migration 0053): OPERATIONAL problems of devices
// -- a camera re-uploading the same file and wasting data, a photo that fell
// back to the expensive video path, clips recovered late -- with tools to act on
// them. Deliberately COMPACT (one line per problem). The database already
// deduplicates: the same problem on the same device is ONE row with a counter.

const DOT: Record<AlarmSeverity, string> = { critical: "#f43f5e", warning: "#f5b83d", info: "#2f93ff" };

function ago(iso: string): string {
  const m = Math.floor((Date.now() - new Date(iso).getTime()) / 60_000);
  if (m < 1) return "recién";
  if (m < 60) return `hace ${m} min`;
  const h = Math.floor(m / 60);
  return h < 24 ? `hace ${h} h` : `hace ${Math.floor(h / 24)} d`;
}

function mb(bytes: number): string {
  return bytes >= 1_000_000 ? `${(bytes / 1_000_000).toFixed(1)} MB` : `${Math.round(bytes / 1000)} KB`;
}

export function DeviceHealthPanel() {
  const { role } = useAuth();
  const isSuperAdmin = role === "super_admin";
  const [items, setItems] = useState<DeviceHealthEvent[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [confirmReboot, setConfirmReboot] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  async function load() {
    try {
      const r = await api.listDeviceHealth({ limit: 100 });
      setItems(r.items);
      setError(null);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "error cargando la salud de equipos");
    }
  }

  useEffect(() => {
    load();
    const t = setInterval(load, 30_000);
    return () => clearInterval(t);
  }, []);

  async function resolve(id: string) {
    setBusy(id);
    try {
      await api.resolveDeviceHealth(id);
      setItems((prev) => prev.filter((e) => e.id !== id));
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "no se pudo marcar como resuelto");
    } finally {
      setBusy(null);
    }
  }

  async function reboot(e: DeviceHealthEvent) {
    setBusy(e.id);
    setConfirmReboot(null);
    try {
      const r = await api.sendDeviceConfigCommand(e.device_id, "reboot", {});
      setNotice(`Reinicio enviado a ${e.device_label}: ${r.device_reply ?? r.status}`);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "no se pudo enviar el reinicio");
    } finally {
      setBusy(null);
    }
  }

  return (
    <Card className="p-2 sm:p-2">
      <div className="flex items-center justify-between px-2 pt-1 pb-2">
        <p className="text-sm font-semibold text-ink">
          Salud de equipos{items.length > 0 && <span className="ml-1.5 text-ink-dim">· {items.length} abiertos</span>}
        </p>
        <span className="text-[11px] text-ink-dim">Solo plataforma</span>
      </div>
      {error && <Alert>{error}</Alert>}
      {notice && <p className="px-2 pb-2 text-xs text-emerald-300">{notice}</p>}
      {items.length === 0 ? (
        <p className="px-2 pb-2 text-xs text-ink-dim">Sin problemas abiertos en los equipos.</p>
      ) : (
        <ul className="divide-y divide-line">
          {items.map((e) => {
            const canConfigure = e.protocol === "gt06" || e.protocol === "gt06_video";
            return (
              <li key={e.id} className="flex flex-wrap items-center gap-x-3 gap-y-1 px-2 py-1.5 text-xs">
                <span className="h-2 w-2 shrink-0 rounded-full" style={{ background: DOT[e.severity] }} aria-label={e.severity} />
                <span className="min-w-0 flex-1 basis-64">
                  <span className="font-medium text-ink">{e.title}</span>
                  <span className="text-ink-dim">
                    {" "}
                    · {e.device_label} · {e.tenant_name}
                  </span>
                </span>
                <span className="shrink-0 font-data text-ink-dim" title={`Primera vez ${new Date(e.first_seen).toLocaleString()}`}>
                  ×{e.occurrences}
                  {e.bytes_wasted > 0 && ` · ${mb(e.bytes_wasted)}`} · {ago(e.last_seen)}
                </span>
                <span className="flex shrink-0 items-center gap-1">
                  <Link to={`/units/${e.device_id}`} className="rounded-lg px-2 py-1 text-ink-dim hover:bg-fg/[0.06] hover:text-ink">
                    Unidad
                  </Link>
                  {canConfigure && (
                    <Link
                      to={`/admin/devices/${e.device_id}/config-commands`}
                      className="rounded-lg px-2 py-1 text-ink-dim hover:bg-fg/[0.06] hover:text-ink"
                    >
                      Configurar
                    </Link>
                  )}
                  {isSuperAdmin && canConfigure &&
                    (confirmReboot === e.id ? (
                      <>
                        <button
                          disabled={busy === e.id}
                          onClick={() => reboot(e)}
                          className="rounded-lg bg-rose-500/15 px-2 py-1 font-medium text-rose-300 hover:bg-rose-500/25"
                        >
                          ¿Reiniciar?
                        </button>
                        <button onClick={() => setConfirmReboot(null)} className="rounded-lg px-2 py-1 text-ink-dim hover:text-ink">
                          No
                        </button>
                      </>
                    ) : (
                      <button
                        onClick={() => setConfirmReboot(e.id)}
                        className="rounded-lg px-2 py-1 text-ink-dim hover:bg-fg/[0.06] hover:text-ink"
                        title="Reinicia el equipo (se desconecta unos segundos)"
                      >
                        Reiniciar
                      </button>
                    ))}
                  <button
                    disabled={busy === e.id}
                    onClick={() => resolve(e.id)}
                    className="rounded-lg px-2 py-1 text-ink-dim hover:bg-fg/[0.06] hover:text-ink"
                  >
                    Resuelto
                  </button>
                </span>
              </li>
            );
          })}
        </ul>
      )}
    </Card>
  );
}
