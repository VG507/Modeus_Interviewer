import { useEffect, useState } from 'react';
import { Toggle, ToggleIcon } from '@ui-sdk/react/toggle';

const storageKey = 'modeus-theme';
type Theme = 'light' | 'dark';

function currentTheme(): Theme {
  return document.documentElement.dataset.theme === 'dark' ? 'dark' : 'light';
}

export function ThemeToggle() {
  const [theme, setTheme] = useState<Theme>(currentTheme);

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    try { localStorage.setItem(storageKey, theme); } catch {}
  }, [theme]);

  return <Toggle className="theme-toggle" pressed={theme === 'dark'}
    onPressedChange={pressed => setTheme(pressed ? 'dark' : 'light')} aria-label="Переключить тему">
    <ToggleIcon>
      <span className="theme-icon">
        <svg className="theme-sun" width="16" height="16" viewBox="0 0 24 24" fill="none"
          stroke="currentColor" strokeWidth="1.5" strokeLinecap="round">
          <circle cx="12" cy="12" r="4"/>
          <path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M4.93 19.07l1.41-1.41M17.66 6.34l1.41-1.41"/>
        </svg>
        <svg className="theme-moon" width="16" height="16" viewBox="0 0 24 24" fill="none"
          stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
          <path d="M12 3a6 6 0 0 0 9 9 9 9 0 1 1-9-9Z"/>
        </svg>
      </span>
    </ToggleIcon>
  </Toggle>;
}
