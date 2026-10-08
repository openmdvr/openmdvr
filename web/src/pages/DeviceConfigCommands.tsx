import { useEffect, useState } from "react";
import { useParams, useSearchParams, Link } from "react-router-dom";
import {
  api,
  ApiError,
  type DeviceConfigCommand,
  type DeviceConfigCommandKey,
  type DeviceConfigCommandStatus,
} from "../lib/api";
import { useAuth } from "../lib/auth";
import { Alert, Badge, Button, Card, CardTitle, EmptyState, Field, Input, PageContainer, PageHeader, Pagination, Select, type BadgeTone } from "../components/ui";

// GT06 CONFIGURATION commands -- see api/app/gt06_config_commands.py for the
// full catalog. Reachable by the platform ONLY (super_admin/support, see the
// guard in App.tsx) -- the link leading here (Dashboard.tsx, Devices table) is
// also gated by isPlatform, same as "Edit" in that table.
//
// SENDING a command (as opposed to just viewing them) is super_admin ONLY -- see
// canSendCommands below, which mirrors client side the SAME rule the backend
// enforces (require_super_admin in device_config_commands.py). A button the
// backend would reject must never be shown enabled.
//
// The real raw GT06 text is ALWAYS built by the backend
// (app/gt06_config_commands.py) from these parameters -- this form never
// composes or sends free text.

type FieldType = "text" | "number" | "checkbox" | "select";

interface FieldDef {
  key: string;
  label: string;
  type: FieldType;
  options?: { value: string; label: string }[];
  defaultValue?: string;
  placeholder?: string;
}

function opts(...values: string[]): { value: string; label: string }[] {
  return values.map((v) => ({ value: v, label: v }));
}

const COMMAND_LABEL: Record<DeviceConfigCommandKey, string> = {
  corekitsw: "Habilitar configuración de video (COREKITSW)",
  server: "Servidor de conexión (SERVER)",
  apn: "APN de datos móviles",
  upload_url: "URL de subida de clips (UPLOAD)",
  filelist_url: "URL de lista de archivos (FILELIST)",
  uploadsw: "Subida automática por tipo de alarma (UPLOADSW)",
  timezone: "Zona horaria (TIMEZONE)",
  timer: "Intervalo de reporte con ignición ON (TIMER)",
  anglerep: "Umbral de reporte por ángulo (ANGLEREP)",
  sosalm: "Activar función de alarma SOS (SOSALM)",
  mileage: "Activar odómetro (MILEAGE)",
  timesync: "Sincronizar hora con GPS (TIMESYNC)",
  timer_acc_off: "Intervalo de reporte con ignición OFF (TIMER1)",
  accrep: "Reportar cambios de ignición como evento (ACCREP)",
  crashalm: "Sensibilidad de alerta de choque (CRASHALM)",
  rapidacc_sensitivity: "Sensibilidad de aceleración brusca (RAPIDACC)",
  rapiddec_sensitivity: "Sensibilidad de frenado brusco (RAPIDDEC)",
  rapidturn_sensitivity: "Sensibilidad de giro brusco (RAPIDTURN)",
  rapidtest: "Umbrales de conducción agresiva (RAPIDTEST)",
  reboot: "Reiniciar el dispositivo (REBOOT)",
  uart: "Sensor cableado puerta/cinturón (UART)",
  rservice: "Destino del video en vivo RTMP (RSERVICE)",
  senalm: "Vibración con ignición encendida (SENALM)",
  recordaudio: "Grabación de audio (RECORDAUDIO)",
  recordaudio_sub: "Grabación de audio secundario/cabina (RECORDAUDIO_SUB)",
  volume: "Volumen del altavoz (VOLUME)",
  exdevicesw: "Lector RFID externo (EXDEVICESW)",
  sensor: "Config. avanzada de sensibilidad de choque (SENSOR)",
  shock: "Config. avanzada de sensibilidad de vibración (SHOCK)",
  mile: "Unidad de velocidad KPH/MPH (MILE)",
  defense_time: "Retraso de modo defensa tras apagar (DEFENSE_TIME)",
  shutdowntime: "Retraso de apagado tras ignición OFF (SHUTDOWNTIME)",
  exbatalm: "Alarma de batería del vehículo baja (EXBATALM)",
  fatigue: "Alerta de conducción por fatiga (FATIGUE)",
  filter: "Filtro de frecuencia de eventos de choque (FILTER)",
  collide: "Tolerancia al impacto (COLLIDE)",
  video_capture: "Grabar clip puntual de cámara (Video)",
  picture_capture: "Tomar foto puntual (Picture)",
  speed: "Alerta nativa de exceso de velocidad (SPEED)",
  update_firmware: "Actualizar firmware (UPDATE)",
  wakeup_query: "Despertar la cámara (WAKEUP_QUERY)",
};

// Plain-language description of what each command does.
const COMMAND_DESCRIPTION: Record<DeviceConfigCommandKey, string> = {
  corekitsw: "Desbloquea la configuración de servidor de video personalizado -- requisito antes de SERVER/APN/UPLOAD/FILELIST/RSERVICE.",
  server: "Fija a dónde se conecta el dispositivo para reportar GPS/alarmas/telemetría (host y puerto del servidor GT06).",
  apn: "Configura el punto de acceso de datos móviles (APN) que el dispositivo debe usar para conectarse a internet.",
  upload_url: "Fija la URL a la que el dispositivo sube los clips de video grabados.",
  filelist_url: "Fija la URL a la que el dispositivo reporta la lista de archivos de video que tiene grabados.",
  uploadsw: "Activa o desactiva que un tipo de alarma (SOS, choque, aceleración/frenado/giro brusco) dispare la subida automática de su clip.",
  timezone: "Fija la zona horaria del dispositivo -- crítico para que los timestamps de los clips grabados coincidan con la hora real.",
  timer: "Cada cuántos segundos reporta su posición mientras el motor está encendido.",
  anglerep: "Reporta la posición si el dispositivo gira más de este ángulo, además del intervalo normal.",
  sosalm: "Activa la función de alarma SOS del propio dispositivo (independiente del botón físico de pánico).",
  mileage: "Activa el odómetro interno del dispositivo, opcionalmente arrancando desde un valor inicial en metros.",
  timesync: "Sincroniza el reloj interno del dispositivo con la hora del satélite GPS en vez de la red celular.",
  timer_acc_off: "Cada cuántos segundos reporta su posición mientras el motor está APAGADO.",
  accrep: "Activa o desactiva que el dispositivo reporte cada cambio de encendido/apagado del motor como un evento propio.",
  crashalm: "Nivel de sensibilidad (1 baja a 3 alta) para que un impacto dispare la alerta de choque.",
  rapidacc_sensitivity: "Nivel de sensibilidad (1 baja a 3 alta) para detectar una aceleración brusca.",
  rapiddec_sensitivity: "Nivel de sensibilidad (1 baja a 3 alta) para detectar un frenado brusco.",
  rapidturn_sensitivity: "Nivel de sensibilidad (1 baja a 3 alta) para detectar un giro brusco.",
  rapidtest: "Umbrales numéricos combinados para la alerta de conducción agresiva (significado exacto de cada valor sin confirmar contra hardware real).",
  reboot: "Reinicia el dispositivo para que la configuración pendiente surta efecto -- provoca una desconexión temporal real.",
  uart: "Configura un sensor cableado (puerta o cinturón de seguridad): cuándo dispara, con qué condición de velocidad/ignición, y qué acción toma.",
  rservice: "Fija a dónde el dispositivo empuja su transmisión de video en vivo (RTMP) -- distinto de SERVER, que es solo para GPS/telemetría.",
  senalm: "Activa una vibración/alarma cuando el motor está encendido, con un nivel de sensibilidad.",
  recordaudio: "Activa o desactiva la grabación de audio del micrófono principal.",
  recordaudio_sub: "Activa o desactiva la grabación de audio del micrófono secundario (cabina).",
  volume: "Nivel de volumen del altavoz del dispositivo, de 0 (silenciado) al máximo.",
  exdevicesw: "Activa o desactiva un lector RFID externo conectado al dispositivo.",
  sensor: "Ajuste avanzado (valor crudo) de la sensibilidad de la alerta de choque -- sin desglose documentado más allá del valor.",
  shock: "Ajuste avanzado (valor crudo) de la sensibilidad de la alerta de vibración -- sin desglose documentado más allá del valor.",
  mile: "Cambia la unidad en la que el dispositivo reporta velocidad, de km/h a mph o de vuelta.",
  defense_time: "Minutos de retraso antes de que el modo defensa (antirrobo) se active después de apagar el motor.",
  shutdowntime: "Minutos de retraso antes de que el dispositivo se apague después de apagar el motor.",
  exbatalm: "Alarma cuando la batería del VEHÍCULO (no la del dispositivo) cae por debajo de un voltaje -- parámetros crudos, sin confirmar contra hardware real.",
  fatigue: "Alerta de conducción por fatiga tras un tiempo continuo de manejo -- parámetros crudos, sin confirmar contra hardware real.",
  filter: "Ventana de tiempo entre eventos de choque para evitar duplicados por la misma sacudida.",
  collide: "Tolerancia al impacto -- 7 valores numéricos crudos sin desglose documentado, tomados literalmente de un ejemplo real de referencia.",
  video_capture: "Dispara de inmediato una grabación puntual de la cámara interior o exterior por la duración indicada -- acción inmediata, no configuración persistente.",
  picture_capture: "Dispara de inmediato una foto de una o ambas cámaras -- acción inmediata, no configuración persistente.",
  speed: "Alerta de exceso de velocidad calculada por el propio dispositivo (independiente del límite de velocidad que ya maneja esta plataforma por vehículo).",
  update_firmware: "Descarga e instala una versión de firmware desde el servidor oficial de Jimi IoT -- puede dejar el dispositivo inoperante si se interrumpe o si se salta una versión intermedia.",
  wakeup_query: "Despierta la cámara cuando entró en reposo tras apagar el vehículo. El equipo no contesta este comando: si funciona, vuelve a reportarse en unos 30 segundos (revisa \"visto hace\"). Jimi lo documenta para JC450/JC181; en la JC261 falta confirmarlo. Solo sirve si la cámara sigue conectada; si ya se desconectó por completo, ningún comando le llega.",
};

// Same four commands as HIGH_RISK_COMMAND_KEYS in app/gt06_config_commands.py --
// they change WHERE the device connects/publishes, or can leave it with broken
// firmware. Same friction as "Cut engine" (DeviceEngineControls): type the
// device label before the button is enabled.
const HIGH_RISK_KEYS: ReadonlySet<DeviceConfigCommandKey> = new Set(["server", "apn", "rservice", "update_firmware"]);

// Groups the picker by source confidence -- 40 ungrouped commands would be
// unreadable. Same rule documented in gt06_config_commands.py: confirmed against
// real hardware > confirmed by a second independent source > single source, not
// yet confirmed.
const COMMAND_GROUPS: { label: string; keys: DeviceConfigCommandKey[] }[] = [
  {
    label: "Confirmados contra el hardware real de este proyecto",
    keys: ["corekitsw", "server", "apn", "upload_url", "filelist_url", "uploadsw", "timezone", "timer", "anglerep", "sosalm"],
  },
  {
    label: "Confirmados por una segunda fuente independiente",
    keys: [
      "mileage", "timesync", "timer_acc_off", "accrep", "crashalm",
      "rapidacc_sensitivity", "rapiddec_sensitivity", "rapidturn_sensitivity",
      "rapidtest", "reboot", "uart", "rservice",
    ],
  },
  {
    label: "Sin confirmar contra hardware real todavía",
    keys: [
      "senalm", "recordaudio", "recordaudio_sub", "volume", "exdevicesw",
      "sensor", "shock", "mile", "defense_time", "shutdowntime", "exbatalm",
      "fatigue", "filter", "collide", "video_capture", "picture_capture",
      "speed", "update_firmware",
    ],
  },
  {
    label: "Energía y reposo (sin confirmar en la JC261)",
    keys: ["wakeup_query"],
  },
];

const COMMAND_FIELDS: Record<DeviceConfigCommandKey, FieldDef[]> = {
  corekitsw: [],
  server: [
    { key: "host", label: "Host o IP del servidor", type: "text", placeholder: "203.0.113.10" },
    { key: "port", label: "Puerto", type: "number", defaultValue: "5023" },
    { key: "mode", label: "Modo", type: "select", options: opts("1", "0"), defaultValue: "1" },
  ],
  apn: [
    { key: "name", label: "Nombre del APN", type: "text", placeholder: "internet" },
    { key: "apn", label: "APN", type: "text", placeholder: "internet" },
    { key: "user", label: "Usuario (opcional)", type: "text" },
    { key: "password", label: "Contraseña (opcional)", type: "text" },
  ],
  upload_url: [
    { key: "host", label: "Host o IP del servidor (el de esta plataforma)", type: "text", placeholder: "203.0.113.10" },
    { key: "port", label: "Puerto", type: "number", defaultValue: "8083" },
  ],
  filelist_url: [
    { key: "host", label: "Host o IP del servidor (el de esta plataforma)", type: "text", placeholder: "203.0.113.10" },
    { key: "port", label: "Puerto", type: "number", defaultValue: "8083" },
  ],
  uploadsw: [
    { key: "alarm_type", label: "Tipo de alarma", type: "select", options: opts("SOS", "CRASH", "RAPIDACC", "RAPIDDEC", "RAPIDTURN") },
    { key: "enabled", label: "Subida automática activada", type: "checkbox" },
  ],
  timezone: [{ key: "offset", label: "Offset (+HH:MM o -HH:MM)", type: "text", placeholder: "-07:00" }],
  timer: [{ key: "seconds", label: "Intervalo en segundos", type: "number", defaultValue: "60" }],
  anglerep: [{ key: "degrees", label: "Umbral de ángulo en grados", type: "number", defaultValue: "10" }],
  sosalm: [],
  mileage: [{ key: "initial_meters", label: "Valor inicial en metros (opcional)", type: "number" }],
  timesync: [],
  timer_acc_off: [{ key: "seconds", label: "Intervalo en segundos", type: "number", defaultValue: "3600" }],
  accrep: [{ key: "enabled", label: "Activado", type: "checkbox" }],
  crashalm: [{ key: "sensitivity", label: "Sensibilidad (1 baja, 3 alta)", type: "select", options: opts("1", "2", "3"), defaultValue: "2" }],
  rapidacc_sensitivity: [{ key: "sensitivity", label: "Sensibilidad (1 baja, 3 alta)", type: "select", options: opts("1", "2", "3"), defaultValue: "2" }],
  rapiddec_sensitivity: [{ key: "sensitivity", label: "Sensibilidad (1 baja, 3 alta)", type: "select", options: opts("1", "2", "3"), defaultValue: "2" }],
  rapidturn_sensitivity: [{ key: "sensitivity", label: "Sensibilidad (1 baja, 3 alta)", type: "select", options: opts("1", "2", "3"), defaultValue: "2" }],
  rapidtest: [
    { key: "accel_threshold", label: "Umbral de aceleración", type: "number", defaultValue: "30" },
    { key: "decel_threshold", label: "Umbral de frenado", type: "number", defaultValue: "40" },
    { key: "turn_threshold", label: "Umbral de giro", type: "number", defaultValue: "70" },
  ],
  reboot: [],
  uart: [
    { key: "trigger_mode", label: "Modo de disparo", type: "select", options: [
      { value: "disabled", label: "Desactivado" },
      { value: "trigger_on_close", label: "Al cerrar" },
      { value: "trigger_on_open", label: "Al abrir" },
    ], defaultValue: "trigger_on_close" },
    { key: "acc_state", label: "Condición de ignición", type: "select", options: [
      { value: "any", label: "Cualquiera" },
      { value: "acc_on", label: "Solo con motor encendido" },
      { value: "acc_off", label: "Solo con motor apagado" },
    ], defaultValue: "any" },
    { key: "interval_seconds", label: "Intervalo entre detecciones (segundos)", type: "number", defaultValue: "60" },
    { key: "max_speed_kmh", label: "Velocidad máxima para contar (0 = sin límite)", type: "number", defaultValue: "100" },
    { key: "action", label: "Acción al disparar", type: "select", options: [
      { value: "short_video", label: "Video corto" },
      { value: "photo", label: "Foto" },
    ], defaultValue: "short_video" },
    { key: "voice_broadcast", label: "Aviso de voz", type: "select", options: [
      { value: "none", label: "Ninguno" },
      { value: "seatbelt", label: "Cinturón de seguridad" },
      { value: "door_sensor", label: "Sensor de puerta" },
    ], defaultValue: "none" },
  ],
  rservice: [
    { key: "host", label: "Host o dominio del servidor RTMP (el de esta plataforma)", type: "text", placeholder: "fleet.example.com" },
    { key: "port", label: "Puerto", type: "number", defaultValue: "1935" },
    { key: "app", label: "App RTMP", type: "text", defaultValue: "live" },
  ],
  senalm: [{ key: "sensitivity", label: "Sensibilidad (1 baja, 3 alta)", type: "select", options: opts("1", "2", "3"), defaultValue: "2" }],
  recordaudio: [{ key: "enabled", label: "Activado", type: "checkbox" }],
  recordaudio_sub: [{ key: "enabled", label: "Activado", type: "checkbox" }],
  volume: [{ key: "level", label: "Nivel (0 a 15)", type: "number", defaultValue: "0" }],
  exdevicesw: [{ key: "enabled", label: "Activado", type: "checkbox" }],
  sensor: [{ key: "value", label: "Valor (0 a 255)", type: "number", defaultValue: "255" }],
  shock: [{ key: "value", label: "Valor", type: "number", defaultValue: "20" }],
  mile: [{ key: "use_mph", label: "Usar millas por hora (MPH)", type: "checkbox" }],
  defense_time: [{ key: "minutes", label: "Minutos de retraso", type: "number", defaultValue: "2" }],
  shutdowntime: [{ key: "minutes", label: "Minutos de retraso", type: "number", defaultValue: "30" }],
  wakeup_query: [],
  exbatalm: [
    { key: "mode", label: "Modo", type: "number", defaultValue: "0" },
    { key: "voltage", label: "Voltaje umbral", type: "number", defaultValue: "115" },
  ],
  fatigue: [
    { key: "param_a", label: "Parámetro A", type: "number", defaultValue: "4" },
    { key: "param_b", label: "Parámetro B", type: "number", defaultValue: "5" },
  ],
  filter: [{ key: "seconds", label: "Segundos", type: "number", defaultValue: "5" }],
  collide: [
    { key: "p1", label: "Parámetro 1", type: "number", defaultValue: "0" },
    { key: "p2", label: "Parámetro 2", type: "number", defaultValue: "225" },
    { key: "p3", label: "Parámetro 3", type: "number", defaultValue: "0" },
    { key: "p4", label: "Parámetro 4", type: "number", defaultValue: "15" },
    { key: "p5", label: "Parámetro 5", type: "number", defaultValue: "6" },
    { key: "p6", label: "Parámetro 6", type: "number", defaultValue: "70" },
    { key: "p7", label: "Parámetro 7", type: "number", defaultValue: "200" },
  ],
  video_capture: [
    { key: "direction", label: "Cámara", type: "select", options: [
      { value: "in", label: "Interior (cabina)" },
      { value: "out", label: "Exterior (frontal)" },
    ] },
    { key: "duration_seconds", label: "Duración en segundos", type: "number", defaultValue: "3" },
  ],
  picture_capture: [
    { key: "mode", label: "Cámara", type: "select", options: [
      { value: "in", label: "Interior (cabina)" },
      { value: "out", label: "Exterior (frontal)" },
      { value: "inout", label: "Ambas" },
    ] },
  ],
  speed: [
    { key: "p1", label: "Parámetro 1", type: "number", defaultValue: "10" },
    { key: "p2", label: "Parámetro 2", type: "number", defaultValue: "90" },
    { key: "p3", label: "Parámetro 3", type: "number", defaultValue: "1" },
  ],
  update_firmware: [
    { key: "url", label: "URL de firmware (jimi-ota...aliyuncs.com)", type: "text", placeholder: "https://jimi-ota.oss-cn-hongkong.aliyuncs.com/JC261_OTA/.../update.zip" },
  ],
};

const STATUS_LABEL: Record<DeviceConfigCommandStatus, string> = {
  pending: "en curso",
  success: "éxito",
  failed: "falló",
  timeout: "sin respuesta",
  device_offline: "sin conexión",
};

const STATUS_TONE: Record<DeviceConfigCommandStatus, BadgeTone> = {
  pending: "brand",
  success: "success",
  failed: "danger",
  timeout: "warning",
  device_offline: "muted",
};

function defaultParamsFor(key: DeviceConfigCommandKey): Record<string, string> {
  const out: Record<string, string> = {};
  for (const f of COMMAND_FIELDS[key]) {
    out[f.key] = f.defaultValue ?? (f.type === "checkbox" ? "false" : f.type === "select" ? f.options![0].value : "");
  }
  return out;
}

function buildParamsPayload(key: DeviceConfigCommandKey, raw: Record<string, string>): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const f of COMMAND_FIELDS[key]) {
    const v = raw[f.key];
    if (f.type === "number") out[f.key] = v === "" ? undefined : Number(v);
    else if (f.type === "checkbox") out[f.key] = v === "true";
    else if (v !== "") out[f.key] = v;
  }
  return out;
}

const PAGE_LIMIT = 20;

export default function DeviceConfigCommands() {
  const { deviceId } = useParams<{ deviceId: string }>();
  const [searchParams] = useSearchParams();
  const deviceLabel = searchParams.get("device_label") ?? "esta unidad";
  const { role } = useAuth();
  // Mirrors client side the SAME rule the backend enforces (require_super_admin)
  // -- support can still see the history below, but never sees a "Send" button
  // the backend would reject with 403.
  const canSendCommands = role === "super_admin";

  const [commandKey, setCommandKey] = useState<DeviceConfigCommandKey>("corekitsw");
  const [params, setParams] = useState<Record<string, string>>(defaultParamsFor("corekitsw"));
  const [confirmText, setConfirmText] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [lastResult, setLastResult] = useState<DeviceConfigCommand | null>(null);

  const [offset, setOffset] = useState(0);
  const [page, setPage] = useState<{ items: DeviceConfigCommand[]; total: number } | null>(null);

  function selectCommand(key: DeviceConfigCommandKey) {
    setCommandKey(key);
    setParams(defaultParamsFor(key));
    setConfirmText("");
    setError(null);
  }

  async function reloadHistory() {
    if (!deviceId) return;
    try {
      setPage(await api.listDeviceConfigCommands(deviceId, { limit: PAGE_LIMIT, offset }));
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "error cargando el historial");
    }
  }

  useEffect(() => {
    reloadHistory();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [deviceId, offset]);

  async function send() {
    if (!deviceId) return;
    setBusy(true);
    setError(null);
    try {
      const result = await api.sendDeviceConfigCommand(deviceId, commandKey, buildParamsPayload(commandKey, params));
      setLastResult(result);
      setOffset(0);
      await reloadHistory();
      setConfirmText("");
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "error enviando el comando");
    } finally {
      setBusy(false);
    }
  }

  if (!deviceId) return null;

  const fields = COMMAND_FIELDS[commandKey];
  const highRisk = HIGH_RISK_KEYS.has(commandKey);
  const canSend = canSendCommands && !busy && (!highRisk || confirmText === deviceLabel);

  return (
    <PageContainer>
      <PageHeader
        title="Configuración del dispositivo"
        description={`Unidad: ${deviceLabel} · comandos de configuración GT06 (solo plataforma)`}
      />

      <Card className="space-y-3">
        <CardTitle>Enviar comando</CardTitle>
        {error && <Alert>{error}</Alert>}
        {!canSendCommands && (
          <Alert>Tu cuenta puede ver el historial de esta unidad, pero solo el rol super_admin puede enviar comandos nuevos.</Alert>
        )}

        <Field label="Comando">
          <Select value={commandKey} onChange={(e) => selectCommand(e.target.value as DeviceConfigCommandKey)} disabled={!canSendCommands}>
            {COMMAND_GROUPS.map((group) => (
              <optgroup key={group.label} label={group.label}>
                {group.keys.map((key) => (
                  <option key={key} value={key}>
                    {COMMAND_LABEL[key]}
                  </option>
                ))}
              </optgroup>
            ))}
          </Select>
        </Field>
        <p className="text-xs text-ink-dim">{COMMAND_DESCRIPTION[commandKey]}</p>

        {fields.map((f) => (
          <Field key={f.key} label={f.label}>
            {f.type === "select" ? (
              <Select value={params[f.key]} onChange={(e) => setParams((p) => ({ ...p, [f.key]: e.target.value }))} disabled={!canSendCommands}>
                {f.options!.map((opt) => (
                  <option key={opt.value} value={opt.value}>
                    {opt.label}
                  </option>
                ))}
              </Select>
            ) : f.type === "checkbox" ? (
              <input
                type="checkbox"
                checked={params[f.key] === "true"}
                onChange={(e) => setParams((p) => ({ ...p, [f.key]: e.target.checked ? "true" : "false" }))}
                disabled={!canSendCommands}
                className="h-4 w-4"
              />
            ) : (
              <Input
                type={f.type === "number" ? "number" : "text"}
                value={params[f.key]}
                placeholder={f.placeholder}
                onChange={(e) => setParams((p) => ({ ...p, [f.key]: e.target.value }))}
                disabled={!canSendCommands}
              />
            )}
          </Field>
        ))}

        {highRisk && canSendCommands && (
          <div className="space-y-2 rounded-sm border border-red-500/40 bg-red-500/5 p-2">
            <p className="text-sm text-ink">
              Este comando puede dejar el device sin poder conectarse a esta plataforma (o sin video en vivo, o con firmware roto) si algo está mal. Escribe{" "}
              <span className="font-data font-semibold text-ink">{deviceLabel}</span> para confirmar.
            </p>
            <Input value={confirmText} onChange={(e) => setConfirmText(e.target.value)} placeholder={deviceLabel} />
          </div>
        )}

        <Button disabled={!canSend} onClick={send}>
          {busy ? "Enviando…" : "Enviar comando"}
        </Button>

        {lastResult && (
          <div className="space-y-1 rounded-sm border border-line-strong bg-surface-2 p-2 text-xs">
            <div className="flex items-center justify-between">
              <span className="font-data text-ink">{lastResult.raw_text}</span>
              <Badge tone={STATUS_TONE[lastResult.status]}>{STATUS_LABEL[lastResult.status]}</Badge>
            </div>
            <p className="text-ink-dim">
              Respuesta del dispositivo: {lastResult.device_reply ? <span className="font-data text-ink">{lastResult.device_reply}</span> : "(sin texto)"}
            </p>
          </div>
        )}
      </Card>

      <Card>
        <CardTitle>Historial de cambios</CardTitle>
        {page == null ? (
          <p className="text-sm text-ink-faint">Cargando…</p>
        ) : page.items.length === 0 ? (
          <EmptyState>Sin comandos de configuración enviados todavía.</EmptyState>
        ) : (
          <>
            <ul className="divide-y divide-line">
              {page.items.map((c) => (
                <li key={c.id} className="space-y-1 py-3">
                  <div className="flex items-center justify-between gap-2">
                    <span className="text-sm font-medium text-ink">{COMMAND_LABEL[c.command_key]}</span>
                    <Badge tone={STATUS_TONE[c.status]}>{STATUS_LABEL[c.status]}</Badge>
                  </div>
                  <p className="font-data text-xs text-ink-dim">{c.raw_text}</p>
                  <p className="text-xs text-ink-dim">
                    Pedido por {c.requested_by_email} · {new Date(c.requested_at).toLocaleString()}
                  </p>
                  <p className="text-xs text-ink-dim">
                    Respuesta del dispositivo: {c.device_reply ? <span className="font-data text-ink">{c.device_reply}</span> : "(sin texto)"}
                  </p>
                </li>
              ))}
            </ul>
            <div className="mt-3">
              <Pagination total={page.total} limit={PAGE_LIMIT} offset={offset} onOffsetChange={setOffset} />
            </div>
          </>
        )}
      </Card>

      <Link to="/admin" className="text-sm font-medium text-brand-700 hover:underline">
        ← Volver a Administración
      </Link>
    </PageContainer>
  );
}
