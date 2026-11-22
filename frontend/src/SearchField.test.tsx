import { useState } from 'react';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { createAgent } from '@ui-sdk/react/agent';
import { expect, test } from 'vitest';
import { SearchField } from './SearchField';

const agent = createAgent();
const clearName = 'Очистить поле «Search vacancies»';

function Example() {
  const [value, setValue] = useState('');
  return <SearchField id="search-vacancies" label="Search vacancies" value={value} onChange={setValue}/>;
}

test('Clear shows only with text, empties the input, and restores focus', async () => {
  const user = userEvent.setup();
  render(<Example/>);
  const input = screen.getByLabelText<HTMLInputElement>('Search vacancies');

  expect(screen.queryByRole('button', { name: clearName })).toBeNull();
  await user.type(input, 'PostgreSQL');
  expect(screen.getByRole('button', { name: clearName })).not.toBeNull();

  await user.click(screen.getByRole('button', { name: clearName }));
  expect(input.value).toBe('');
  expect(document.activeElement).toBe(input);
  expect(screen.queryByRole('button', { name: clearName })).toBeNull();
});

test('an agent can find, fill, and clear the search field', async () => {
  render(<Example/>);
  const components = agent.snapshot().filter(entry => !entry.offPage && entry.component !== 'more');
  for (const entry of components) {
    expect(entry.label, `${entry.component} ${entry.ref} has no name`).toBeTruthy();
  }
  const labels = components.map(entry => entry.label);
  expect(labels).toContain('Search vacancies');

  const result = await agent.act({ steps: [
    { ref: 'Search vacancies', action: 'setValue', args: ['React'] },
    { ref: 'Search vacancies', action: 'clear' },
  ] });
  expect(result.ok, result.error?.message).toBe(true);
  expect(screen.getByLabelText<HTMLInputElement>('Search vacancies').value).toBe('');
});
