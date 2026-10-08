// Translates alarm_type (the raw value stored in the database, see
// jt808-server/internal/gt06server/handlers.go and
// jt808-server/internal/jt808server/location.go) into readable text instead of
// showing the internal enum. Explicit fallback (never blank) for any unlisted
// type: replaces underscores with spaces and strips the known gt06_/jt808
// prefix, so a new type never breaks the UI before it is added here.
const ALARM_TYPE_LABELS: Record<string, string> = {
  // GT06 (jt808-server/internal/gt06server/handlers.go)
  gt06_sos: "Botón de pánico (SOS)",
  gt06_power_cut: "Corte de energía",
  gt06_vibration: "Vibración detectada",
  gt06_geofence_enter: "Entró a geocerca",
  gt06_geofence_exit: "Salió de geocerca",
  gt06_overspeed: "Exceso de velocidad",
  gt06_movement: "Movimiento detectado",
  gt06_gps_blind_area_enter: "Entró a zona sin GPS",
  gt06_gps_blind_area_exit: "Salió de zona sin GPS",
  gt06_power_on: "Encendido",
  gt06_external_power_low: "Batería externa baja",
  gt06_external_power_low_protection: "Protección por batería externa baja",
  gt06_power_off: "Apagado",
  gt06_tampering: "Manipulación del dispositivo",
  gt06_door: "Puerta abierta",
  gt06_low_power_shutdown: "Apagado por batería baja",
  gt06_rapid_acceleration: "Aceleración brusca",
  gt06_collision: "Colisión detectada",
  gt06_flip: "Volcadura detectada",
  gt06_harsh_braking: "Frenado brusco",
  gt06_sharp_turn: "Giro brusco",
  gt06_camera_event: "Evento de cámara detectado",
  gt06_unknown: "Alarma desconocida",

  // JT808 (jt808-server/internal/jt808server/location.go)
  emergency: "Emergencia",
  over_speed: "Exceso de velocidad",
  fatigue_driving: "Conducción con fatiga",
  dangerous_driving: "Conducción peligrosa",
  gnss_module_fault: "Falla del módulo GNSS",
  gnss_antenna_fault: "Falla de antena GNSS",
  gnss_antenna_short_circuit: "Cortocircuito de antena GNSS",
  terminal_power_undervoltage: "Bajo voltaje del terminal",
  terminal_power_shutdown: "Apagado del terminal",
  terminal_lcd_fault: "Falla de pantalla del terminal",
  tts_module_fault: "Falla del módulo de voz",
  camera_fault: "Falla de cámara",
  ic_card_module_fault: "Falla del módulo de tarjeta IC",
  over_speed_warning: "Alerta de exceso de velocidad",
  fatigue_driving_warning: "Alerta de fatiga",
  violation_driving_warning: "Alerta de conducción irregular",
  tire_pressure_warning: "Alerta de presión de llantas",
  right_turn_blind_area_warning: "Alerta de punto ciego (giro derecha)",
  daily_driving_timeout: "Límite diario de conducción excedido",
  overtime_parking: "Estacionamiento prolongado",
  area_in_out: "Entrada/salida de área",
  route_in_out: "Entrada/salida de ruta",
  section_driving_time_abnormal: "Tiempo de tramo anormal",
  route_deviation: "Desviación de ruta",
  vss_fault: "Falla del sensor de velocidad",
  fuel_level_abnormal: "Nivel de combustible anormal",
  vehicle_theft: "Robo de vehículo",
  illegal_ignition: "Encendido no autorizado",
  illegal_displacement: "Desplazamiento no autorizado",
  collision_warning: "Alerta de colisión",
  rollover_warning: "Alerta de volcadura",
  illegal_door_open: "Apertura de puerta no autorizada",

  // Ignition/engine cut (protocol-agnostic, see 0048_device_status_alarms.sql --
  // fires the same for JT808 and GT06, not in the maps above).
  // "power_connected"/"power_cut" are historical internal alarm_type names (kept
  // to avoid rewriting stored rows) for what is actually the fuel/engine cut-off
  // relay, not the vehicle's electrical circuit -- see POWER_TOOLTIP in
  // DeviceDetailPanel.tsx.
  ignition_on: "Ignición encendida",
  ignition_off: "Ignición apagada",
  power_connected: "Corte de motor liberado",
  power_cut: "Corte de motor activado",

  // Geofences (0052_geofences.sql) -- protocol-agnostic, evaluated in
  // insert_gps_position(). The geofence name travels in the notification `body`.
  geofence_enter: "Entrada a geocerca",
  geofence_exit: "Salida de geocerca",
  geofence_dwell: "Permanencia en geocerca",

  // Per-unit maximum speed (protocol-agnostic, see 0050_vehicle_max_speed.sql --
  // fires the same for JT808 and GT06; the check lives in insert_gps_position(),
  // not in per-protocol code)
  overspeed_limit: "Exceso de velocidad (límite de la unidad)",
};

export function alarmTypeLabel(alarmType: string): string {
  const known = ALARM_TYPE_LABELS[alarmType];
  if (known) return known;
  return alarmType
    .replace(/^gt06_/, "")
    .replace(/_/g, " ")
    .replace(/^\w/, (c) => c.toUpperCase());
}

// notifications.title is stored as raw text ("Alarm: " + alarm_type, see
// 0033_notifications.sql) -- translated here instead of in the database to avoid
// rewriting stored history or duplicating the label map in SQL. Any title that
// does NOT follow that pattern (a future event_type) is shown as-is.
const RAW_ALARM_TITLE_PREFIX = "Alarm: ";

// Raw alarm_type of an alarm notification, or null if the title does not follow
// the "Alarm: <type>" pattern.
export function alarmTypeFromNotificationTitle(title: string): string | null {
  return title.startsWith(RAW_ALARM_TITLE_PREFIX) ? title.slice(RAW_ALARM_TITLE_PREFIX.length) : null;
}

export function notificationTitleLabel(title: string): string {
  if (!title.startsWith(RAW_ALARM_TITLE_PREFIX)) return title;
  const alarmType = title.slice(RAW_ALARM_TITLE_PREFIX.length);
  return alarmTypeLabel(alarmType);
}
