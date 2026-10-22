import { useTranslation } from 'react-i18next';
import i18n from '../i18n';

export function LanguageSwitcher({ expanded = true }: { expanded?: boolean }) {
  const { i18n: instance } = useTranslation();
  const isEs = instance.language === 'es';

  function toggle() {
    const next = isEs ? 'en' : 'es';
    i18n.changeLanguage(next);
    try { localStorage.setItem('omd-lang', next); } catch {}
  }

  const label = isEs ? 'EN' : 'ES';
  const title = isEs ? 'Switch to English' : 'Cambiar a español';

  return (
    <button
      onClick={toggle}
      title={title}
      aria-label={title}
      className={`flex h-9 items-center gap-2 rounded-xl text-xs text-ink-faint hover:bg-fg/[0.05] hover:text-ink ${
        expanded ? 'w-full px-3' : 'w-full justify-center'
      }`}
    >
      <span className="shrink-0 text-[11px] font-semibold tracking-wider">{label}</span>
      {expanded && <span>{isEs ? 'English' : 'Español'}</span>}
    </button>
  );
}

export function LanguageSwitcherListItem() {
  const { i18n: instance } = useTranslation();
  const isEs = instance.language === 'es';

  function toggle() {
    const next = isEs ? 'en' : 'es';
    i18n.changeLanguage(next);
    try { localStorage.setItem('omd-lang', next); } catch {}
  }

  return (
    <button
      onClick={toggle}
      className="w-full rounded-xl border border-line-strong bg-fg/[0.04] px-4 py-2.5 text-left text-sm font-medium text-ink hover:bg-fg/[0.08]"
    >
      {isEs ? 'Switch to English' : 'Cambiar a español'}
    </button>
  );
}
