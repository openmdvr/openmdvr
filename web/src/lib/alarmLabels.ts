import i18next from 'i18next';

// Translates alarm_type (raw DB value) into readable text. Uses i18next for
// locale-aware translation, with a fallback that humanizes unknown types.
export function alarmTypeLabel(alarmType: string): string {
  const key = `alarms.${alarmType}`;
  const translated = i18next.t(key, { defaultValue: '' });
  if (translated) return translated;
  return alarmType
    .replace(/^gt06_/, '')
    .replace(/_/g, ' ')
    .replace(/^\w/, (c) => c.toUpperCase());
}

// notifications.title is stored as "Alarm: <alarm_type>" -- translated here
// instead of in the database.
const RAW_ALARM_TITLE_PREFIX = 'Alarm: ';

export function alarmTypeFromNotificationTitle(title: string): string | null {
  return title.startsWith(RAW_ALARM_TITLE_PREFIX) ? title.slice(RAW_ALARM_TITLE_PREFIX.length) : null;
}

export function notificationTitleLabel(title: string): string {
  if (!title.startsWith(RAW_ALARM_TITLE_PREFIX)) return title;
  const alarmType = title.slice(RAW_ALARM_TITLE_PREFIX.length);
  return alarmTypeLabel(alarmType);
}
