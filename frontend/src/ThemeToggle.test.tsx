import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { createAgent } from '@ui-sdk/react/agent';
import { beforeEach, expect, test } from 'vitest';
import { ThemeToggle } from './ThemeToggle';

const agent = createAgent();

beforeEach(() => {
  localStorage.clear();
  document.documentElement.dataset.theme = 'light';
});

test('переключатель включает тёмную тему, сохраняет выбор и возвращает светлую', async () => {
  const user = userEvent.setup();
  render(<ThemeToggle/>);
  const toggle = screen.getByRole('button', { name: 'Переключить тему' });

  expect(toggle.getAttribute('aria-pressed')).toBe('false');
  await user.click(toggle);
  expect(document.documentElement.dataset.theme).toBe('dark');
  expect(toggle.getAttribute('aria-pressed')).toBe('true');
  expect(localStorage.getItem('modeus-theme')).toBe('dark');

  await user.click(toggle);
  expect(document.documentElement.dataset.theme).toBe('light');
  expect(toggle.getAttribute('aria-pressed')).toBe('false');
  expect(localStorage.getItem('modeus-theme')).toBe('light');
});

test('агент находит и переключает тему', async () => {
  render(<ThemeToggle/>);
  const components = agent.snapshot().filter(entry => !entry.offPage && entry.component !== 'more');
  for (const entry of components) {
    expect(entry.label, `${entry.component} ${entry.ref} has no name`).toBeTruthy();
  }
  expect(components.map(entry => entry.label)).toContain('Переключить тему');

  const result = await agent.act({ steps: [
    { ref: 'Переключить тему', action: 'setPressed', args: [true] },
  ] });
  expect(result.ok, result.error?.message).toBe(true);
  expect(document.documentElement.dataset.theme).toBe('dark');
});
