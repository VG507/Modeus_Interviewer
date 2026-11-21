import { useEffect, useState, type FormEvent } from 'react';
import { api, ApiError, command, describe } from './api';

type Watch = {
  id: string; title: string; url: string; enabled: boolean;
  checked_at: string | null; attempted_at: string | null; error: string | null;
};
type Run = { collected: number; changed: number; busy: number; errors: Record<string, string> };
const timestamp = (value: string) => new Date(value).toLocaleString('ru-RU', { timeZone: 'Europe/Moscow' });

export function DocumentationSettings({ profession, refresh, onCollected, onSessionExpired }: {
  profession: string; refresh: number; onCollected: () => void; onSessionExpired: () => void;
}) {
  const [watches, setWatches] = useState<Watch[]>([]);
  const [targets, setTargets] = useState<string[]>([]);
  const [working, setWorking] = useState<'save' | 'collect' | null>(null);
  const [error, setError] = useState('');
  const [result, setResult] = useState<Run | null>(null);
  const [saved, setSaved] = useState(false);
  const [runCount, setRunCount] = useState(0);
  const [loaded, setLoaded] = useState(false);
  const dirty = watches.some(watch => watch.enabled !== targets.includes(watch.id));

  useEffect(() => {
    const request = new AbortController();
    const params = new URLSearchParams({ profession_id: profession });
    api<Watch[]>('/discovery/documentation?' + params, { signal: request.signal }).then(data => {
      if (request.signal.aborted) return;
      setWatches(data);
      setTargets(data.filter(watch => watch.enabled).map(watch => watch.id));
      setLoaded(true);
    }).catch(problem => {
      if (request.signal.aborted) return;
      if (problem instanceof ApiError && problem.status === 401) onSessionExpired();
      else { setError(describe(problem)); setLoaded(true); }
    });
    return () => request.abort();
  }, [profession, refresh, runCount, onSessionExpired]);

  async function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setWorking('save'); setError(''); setResult(null); setSaved(false);
    try {
      const data = await command<Watch[]>('/discovery/documentation', {
        profession_id: profession, targets,
      }, undefined, 'PUT');
      setWatches(data); setSaved(true);
      onCollected();
    } catch (problem) {
      if (problem instanceof ApiError && problem.status === 401) onSessionExpired();
      else setError(describe(problem));
    } finally {
      setWorking(null);
    }
  }

  async function collect() {
    setWorking('collect'); setError(''); setResult(null); setSaved(false);
    try {
      setResult(await command<Run>('/discovery/documentation/collect', { profession_id: profession }));
      setRunCount(value => value + 1);
      onCollected();
    } catch (problem) {
      if (problem instanceof ApiError && problem.status === 401) onSessionExpired();
      else setError(describe(problem));
    } finally {
      setWorking(null);
    }
  }

  return <form className="stack" onSubmit={save}>
    {!loaded && <p role="status">Загружаем список официальных источников…</p>}
    {watches.length > 0 && <fieldset className="stack" disabled={working !== null}>
      <legend>Отслеживаемые руководства и релизы</legend>
      {watches.map(watch => <div key={watch.id}>
        <label className="documentation-option">
          <input type="checkbox" checked={targets.includes(watch.id)} onChange={event => {
            const checked = event.target.checked;
            setSaved(false);
            setTargets(current => checked ? [...current, watch.id] : current.filter(id => id !== watch.id));
          }}/>
          {watch.title}
        </label>
        <p className="meta">
          <a href={watch.url} target="_blank" rel="noreferrer">Официальный источник</a>
          {watch.checked_at && ' · Успешная проверка ' + timestamp(watch.checked_at)}
          {watch.error && watch.attempted_at && ' · Последняя попытка ' + timestamp(watch.attempted_at)}
        </p>
        {watch.enabled && (result?.errors[watch.id] || watch.error) &&
          <p role="alert">{result?.errors[watch.id] || watch.error}</p>}
      </div>)}
      <div className="actions">
        <button type="submit" className="secondary" disabled={!dirty}>Сохранить список</button>
        <button type="button" disabled={dirty || !targets.length} onClick={collect}>Проверить сейчас</button>
      </div>
    </fieldset>}
    {working && <p role="status">{working === 'collect' ? 'Проверяем официальные источники…' : 'Сохраняем список…'}</p>}
    {saved && <p role="status">Список сохранён.</p>}
    {result && <p role="status">Разделов и заметок получено {result.collected}. Новых или изменённых {result.changed}.
      {result.busy > 0 && ' Часть источников уже проверяется.'}</p>}
    {error && <p role="alert">{error}</p>}
  </form>;
}

type ReviewItem = {
  id: string; digest: string; status: string; reason: string | null;
  payload: { kind?: string; reason?: string; supported?: boolean | null };
};

export function DocumentationReview<T extends ReviewItem>({ item, profession, onReviewed, onSessionExpired }: {
  item: T; profession: string; onReviewed: (item: T) => void; onSessionExpired: () => void;
}) {
  const [decision, setDecision] = useState(item.payload.kind === 'documentation' && !item.payload.reason ?
    'accepted' : 'observe');
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  const canAccept = item.payload.kind === 'documentation' && !item.payload.reason;

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const data = new FormData(event.currentTarget);
    const field = (name: string) => String(data.get(name) ?? '').trim();
    const evidence = decision === 'accepted' ? {
      task: field('task'), quote: field('quote'), detail: field('detail'),
      conditions: field('conditions').split('\n').map(value => value.trim()).filter(Boolean),
    } : null;
    if (evidence?.conditions.some(value => value.length > 200) || (evidence?.conditions.length ?? 0) > 8) {
      setError('Укажите до восьми условий длиной не более 200 символов каждое.');
      return;
    }
    setError(''); setSaving(true);
    try {
      onReviewed(await command<T>('/discovery/documentation/' + encodeURIComponent(item.id) + '/review', {
        profession_id: profession, digest: item.digest, decision, reason: field('reason'), evidence,
      }));
    } catch (problem) {
      if (problem instanceof ApiError && problem.status === 401) onSessionExpired();
      else setError(describe(problem));
    } finally {
      setSaving(false);
    }
  }

  return <details>
    <summary>Решение редактора</summary>
    <form onSubmit={submit}>
      <fieldset className="stack" disabled={saving}>
        <label>Решение <select value={decision} onChange={event => setDecision(event.target.value)}>
          {canAccept && <option value="accepted">Допустить выдержку к интервью</option>}
          <option value="observe">Оставить для наблюдения</option>
          <option value="rejected">Отклонить</option>
        </select></label>
        {decision === 'accepted' && <>
          <label>Рабочая задача <input name="task" maxLength={300} required /></label>
          <label>Дословная цитата из раздела <textarea name="quote" maxLength={500} required /></label>
          <label>Фрагмент цитаты для уточнения <input name="detail" minLength={4} maxLength={200} required /></label>
          <label>Условия применимости, по одному на строку
            <textarea name="conditions" maxLength={1607} required={item.payload.supported === false} />
          </label>
        </>}
        <label>Основание для решения <textarea name="reason" maxLength={200} required /></label>
        <button type="submit">{saving ? 'Сохраняем…' : 'Сохранить решение'}</button>
      </fieldset>
      {error && <p role="alert">{error}</p>}
    </form>
  </details>;
}
