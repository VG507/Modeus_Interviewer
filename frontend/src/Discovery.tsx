import { useCallback, useEffect, useRef, useState, type FormEvent } from 'react';
import { api, ApiError, describe } from './api';
import { DocumentationReview, DocumentationSettings } from './Documentation';
import { ProfessionCollection } from './ProfessionCollection';

const searchProviders = { github: 'GitHub Issues', trends: 'GitHub Trends' } as const;
const providers = { ...searchProviders, docs: 'Официальная документация', openalex: 'Научные статьи' } as const;
type Provider = keyof typeof providers;
const statuses: Record<string, string> = {
  new: 'Новый', review: 'На проверке', accepted: 'Допущен к интервью', observe: 'Наблюдаем',
  automatic_check: 'Ожидает автопроверки', rejected: 'Отклонён', expired: 'Срок проверки истёк', unavailable: 'Источник недоступен',
};
const pageSize = 20;
const searchFields = {
  github: { label: 'Репозитории', example: 'facebook/react, vitejs/vite' },
  trends: { label: 'Темы GitHub', example: 'react, frontend, vite' },
};
type Profile = { id: string; title: string };
type Item = {
  id: string; digest: string; title: string; url: string; provider: Provider; observed_at: string; status: string; reason: string | null;
  weekly_star_change?: number | null;
  assessment: {
    resolution_url?: string; resolution_quote?: string; checked_at?: string;
    task?: string; quote?: string; method?: string; sample?: string; limitation?: string; conditions?: string[];
    sections?: Record<string, string>;
  } | null;
};
type ItemPage = { items: Item[]; next_offset: number | null };
type ItemDetail = Item & {
  documentation_diff?: string[] | null;
  payload: {
    repo?: string; number?: number; labels?: string[]; created_at?: string; closed_at?: string;
    version?: string;
    description?: string; stars?: number; forks?: number; language?: string; topics?: string[];
    fragments?: Record<string, { url: string; text: string }>;
    kind?: string; reason?: string; supported?: boolean | null;
    doi?: string; venue?: string; authors?: string[]; publication_date?: string; review_url?: string;
    crossref_checked?: boolean; full_text_sha256?: string; published_licenses?: string[];
  };
};

function date(value?: string) {
  return value ? new Date(value).toLocaleDateString('ru-RU', { timeZone: 'Europe/Moscow' }) : null;
}

function fragmentLabel(key: string) {
  if (key.startsWith('comment:')) return 'Комментарий';
  if (key.startsWith('pull:')) return 'Связанное исправление';
  const labels: Record<string, string> = {
    body: 'Описание Issue', readme: 'README', section: 'Текст раздела',
  };
  return labels[key] ?? key;
}

function Detail({ item }: { item: ItemDetail }) {
  const data = item.payload;
  if (item.provider === 'openalex') {
    const evidence = item.assessment;
    const excerpts = [
      { key: 'quote', label: 'Результат', text: evidence?.quote },
      { key: 'method', label: 'Методика', text: evidence?.method },
      { key: 'sample', label: 'Выборка', text: evidence?.sample },
      { key: 'limitation', label: 'Ограничение', text: evidence?.limitation },
    ];
    return <div className="stack">
      <p className="meta">{[data.venue, date(data.publication_date), data.doi && `DOI ${data.doi}`,
        evidence?.checked_at && `Проверено ${date(evidence.checked_at)}`].filter(Boolean).join(' · ')}</p>
      {!!data.authors?.length && <p className="meta">{data.authors.join(', ')}</p>}
      {item.reason && <p>{item.reason}</p>}
      {data.review_url && <p><a href={data.review_url} target="_blank" rel="noreferrer">Основание проверки рецензирования</a></p>}
      {data.crossref_checked && <p className="meta">На дату проверки Crossref не вернул уведомлений об обновлении.</p>}
      {data.full_text_sha256 && <p className="meta">Источник выдержек — полный текст TEI XML.
        {data.published_licenses?.length ? ` Лицензия опубликованной версии: ${data.published_licenses.join(', ')}.` : ''}</p>}
      {evidence?.task && <p><strong>Рабочая задача</strong> {evidence.task}</p>}
      {!!evidence?.conditions?.length && <p><strong>Условия применимости</strong> {evidence.conditions.join(' · ')}</p>}
      {excerpts.filter(entry => entry.text).map(({ key, label, text }) => <details key={key} open={key === 'method'}>
        <summary>{label}{evidence?.sections?.[key] && ` · ${evidence.sections[key]}`}</summary>
        <p className="discovery-fragment">{text}</p>
      </details>)}
    </div>;
  }
  return <div className="stack">
    <p className="meta">
      {[data.repo && `${data.repo} #${data.number}`, date(data.closed_at) && `Закрыто ${date(data.closed_at)}`,
        data.version && `Версия ${data.version}`,
        data.stars !== undefined && `Звёзды: ${data.stars}`, data.forks !== undefined && `Форки: ${data.forks}`,
        data.language, data.supported === false && 'Поддержка версии завершена',
        item.assessment?.checked_at && 'Проверено ' + date(item.assessment.checked_at)].filter(Boolean).join(' · ')}
    </p>
    {data.description && <p>{data.description}</p>}
    {(data.labels ?? data.topics)?.length ?
      <p className="meta">{(data.labels ?? data.topics)?.join(' · ')}</p> : null}
    {item.reason && <p>{item.reason}</p>}
    {item.assessment?.resolution_url && <p>
      <a href={item.assessment.resolution_url} target="_blank" rel="noreferrer">Подтверждение исправления</a>
      {item.assessment.resolution_quote && <> · {item.assessment.resolution_quote}</>}
    </p>}
    {!!item.documentation_diff?.length && <details>
      <summary>Изменения в источнике</summary>
      <pre className="discovery-fragment">{item.documentation_diff.join('\n')}</pre>
    </details>}
    {Object.entries(data.fragments ?? {}).map(([key, fragment]) => <details key={key}
      open={key === 'body' || key === 'section'}>
      <summary>{fragmentLabel(key)}</summary>
      <p className="discovery-fragment">{fragment.text}</p>
      <a href={fragment.url} target="_blank" rel="noreferrer">Открыть фрагмент в источнике</a>
    </details>)}
  </div>;
}

export function Discovery({ onSessionExpired }: { onSessionExpired: () => void }) {
  const [profiles, setProfiles] = useState<Profile[] | null>(null);
  const [profession, setProfession] = useState('');
  const [provider, setProvider] = useState<Provider | ''>('');
  const [offset, setOffset] = useState(0);
  const [page, setPage] = useState<ItemPage | null>(null);
  const [selected, setSelected] = useState('');
  const [detail, setDetail] = useState<ItemDetail | null>(null);
  const [error, setError] = useState('');
  const [detailError, setDetailError] = useState('');
  const [reload, setReload] = useState(0);
  const [collectionSource, setCollectionSource] = useState<keyof typeof searchProviders>('github');
  const [collectionResult, setCollectionResult] = useState('');
  const [collectionError, setCollectionError] = useState('');
  const [collecting, setCollecting] = useState(false);
  const collectionRequest = useRef<AbortController | null>(null);
  const refreshMaterials = useCallback(() => setReload(value => value + 1), []);

  useEffect(() => () => collectionRequest.current?.abort(), []);

  useEffect(() => {
    const { signal, abort } = controller();
    api<Profile[]>('/discovery/profiles', { signal }).then(result => {
      if (signal.aborted) return;
      setProfiles(result);
      setProfession(current => result.some(profile => profile.id === current) ? current :
        result.find(profile => profile.id === 'python_backend')?.id ?? result[0]?.id ?? '');
    }).catch(problem => handle(problem, signal, setError, onSessionExpired));
    return abort;
  }, [onSessionExpired, reload]);

  useEffect(() => {
    if (!profession) return;
    const { signal, abort } = controller();
    const query = new URLSearchParams({ profession_id: profession, offset: String(offset), limit: String(pageSize) });
    if (provider) query.set('provider', provider);
    api<ItemPage>(`/discovery?${query}`, { signal }).then(result => {
      if (!signal.aborted) setPage(result);
    }).catch(problem => handle(problem, signal, setError, onSessionExpired));
    return abort;
  }, [profession, provider, offset, reload, onSessionExpired]);

  useEffect(() => {
    if (!selected || !profession) return;
    const { signal, abort } = controller();
    const query = new URLSearchParams({ profession_id: profession });
    api<ItemDetail>(`/discovery/items/${encodeURIComponent(selected)}?${query}`, { signal }).then(result => {
      if (!signal.aborted) setDetail(result);
    }).catch(problem => handle(problem, signal, setDetailError, onSessionExpired));
    return abort;
  }, [selected, profession, reload, onSessionExpired]);

  const awaitingAutomaticCheck = page?.items.some(item => item.status === 'automatic_check');
  useEffect(() => {
    if (!awaitingAutomaticCheck) return;
    const timer = window.setInterval(() => setReload(value => value + 1), 15000);
    return () => window.clearInterval(timer);
  }, [awaitingAutomaticCheck]);

  function selectItem(id: string) {
    setDetail(null); setDetailError(''); setSelected(id);
  }

  function showPage(next: number) {
    selectItem(''); setPage(null); setError(''); setOffset(next);
  }

  async function collectForProfession(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!profession || collectionRequest.current) return;
    const data = new FormData(event.currentTarget);
    const terms = (name: string) => String(data.get(name) ?? '').split(/[,\n]/).map(value => value.trim()).filter(Boolean);
    const values = terms('values');
    const labels = terms('labels');
    if (!values.length || values.length > 5 || labels.length > 10) {
      setCollectionError('Укажите от одного до пяти репозиториев или тем и до десяти меток.');
      return;
    }
    const request = new AbortController();
    collectionRequest.current = request;
    setCollecting(true); setCollectionResult(''); setCollectionError('');
    try {
      const result = await api<{ collected: number; partial: boolean }>('/discovery/collect', {
        method: 'POST', signal: request.signal, headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ profession_id: profession, provider: collectionSource, values, labels,
          limit: Number(data.get('limit')) }),
      });
      if (request.signal.aborted) return;
      setCollectionResult(result.collected ? `Сохранено или обновлено записей: ${result.collected}.${result.partial ?
        ' Поиск обработал ограниченную часть выдачи.' : ''}` : result.partial ?
        'В просмотренной части выдачи совпадений нет.' : 'По этим условиям совпадений нет.');
      setProvider(collectionSource); showPage(0); setReload(value => value + 1);
    } catch (problem) {
      if (request.signal.aborted) return;
      if (problem instanceof ApiError && problem.status === 401) onSessionExpired();
      else setCollectionError(describe(problem));
    } finally {
      collectionRequest.current = null;
      if (!request.signal.aborted) setCollecting(false);
    }
  }

  return <section className="stack">
    <h1>Источники</h1>
    <div className="panel vacancy-filters">
      <label>Профессия <select value={profession} disabled={collecting || !profiles?.length} onChange={event => {
        showPage(0); setProfession(event.target.value);
        setCollectionResult(''); setCollectionError('');
      }}>
        {!profiles?.length && <option value="">Нет профилей</option>}
        {profiles?.map(profile => <option key={profile.id} value={profile.id}>{profile.title || profile.id}</option>)}
      </select></label>
      <label>Источник <select value={provider} onChange={event => {
        showPage(0); setProvider(event.target.value as Provider | '');
      }}>
        <option value="">Все источники</option>
        {Object.entries(providers).map(([id, label]) => <option key={id} value={id}>{label}</option>)}
      </select></label>
      <button className="secondary" onClick={() => { showPage(0); setReload(value => value + 1); }}>Обновить</button>
    </div>
    {profession && <ProfessionCollection key={profession} profession={profession}
      onSessionExpired={onSessionExpired} onCompleted={refreshMaterials}/>}
    <form className="panel" onSubmit={collectForProfession}>
      <h2>Сбор для выбранной профессии</h2>
      <fieldset className="vacancy-filters" disabled={collecting || !profession}>
        <label>Сборщик <select value={collectionSource} onChange={event => {
          setCollectionSource(event.target.value as keyof typeof searchProviders); setCollectionResult(''); setCollectionError('');
        }}>
          {Object.entries(searchProviders).map(([id, label]) => <option key={id} value={id}>{label}</option>)}
        </select></label>
        <label key={collectionSource}>{searchFields[collectionSource].label}
          <input name="values" required maxLength={700} placeholder={searchFields[collectionSource].example}/>
        </label>
        {collectionSource === 'github' && <label>Метки Issues <input name="labels" maxLength={500}
          placeholder="bug, performance, database, regression"/></label>}
        <label>Лимит за запуск <input name="limit" type="number" min={1} max={20} defaultValue={10} required/></label>
        <button type="submit">{collecting ? 'Сбор выполняется…' : 'Собрать'}</button>
      </fieldset>
      {collecting && <p role="status">Дождитесь результата. При ошибке сохранённая подборка останется доступной.</p>}
      {collectionError && <p role="alert">{collectionError}</p>}
      {collectionResult && <p role="status">{collectionResult}</p>}
    </form>
    {profession && <details className="panel">
      <summary>Мониторинг официальной документации</summary>
      <DocumentationSettings key={profession} profession={profession} refresh={reload} onSessionExpired={onSessionExpired}
        onCollected={() => { setProvider('docs'); showPage(0); setReload(value => value + 1); }}/>
    </details>}
    {error && <p role="alert">{error}</p>}
    {!error && !page && (profiles === null || profession) && <p role="status">Загружаем материалы…</p>}
    {page && !page.items.length && <p>По выбранным фильтрам материалов пока нет.</p>}
    {page?.items.map(item => <article className="panel" key={item.id}>
      <p className="meta">{providers[item.provider]} · {statuses[item.status] ?? item.status} · Собрано {date(item.observed_at)}
        {item.weekly_star_change != null && ` · Звёзды за неделю: ${item.weekly_star_change > 0 ? '+' : ''}${item.weekly_star_change}`}
      </p>
      <h2><a className="card-link" href={item.url} target="_blank" rel="noreferrer">{item.title}</a></h2>
      <button className="secondary" aria-expanded={selected === item.id}
        onClick={() => selectItem(selected === item.id ? '' : item.id)}>
        {selected === item.id ? 'Скрыть детали' : 'Показать детали'}
      </button>
      {selected === item.id && (detailError ? <p role="alert">{detailError}</p> : detail ? <>
        <Detail item={detail}/>
        {detail.provider === 'docs' && <DocumentationReview key={detail.digest + ':' + detail.status}
          item={detail} profession={profession} onSessionExpired={onSessionExpired} onReviewed={updated => {
            setDetail(updated);
            setPage(current => current && { ...current, items: current.items.map(entry => entry.id === updated.id ? updated : entry) });
          }}/>}
      </> :
        <p role="status">Загружаем детали…</p>)}
    </article>)}
    {page && (offset > 0 || page.next_offset !== null) && <nav className="actions" aria-label="Страницы источников">
      <button className="secondary" disabled={offset === 0} onClick={() => showPage(Math.max(0, offset - pageSize))}>Назад</button>
      <button className="secondary" disabled={page.next_offset === null}
        onClick={() => { if (page.next_offset !== null) showPage(page.next_offset); }}>Дальше</button>
    </nav>}
  </section>;
}

function controller() {
  const request = new AbortController();
  return { signal: request.signal, abort: () => request.abort() };
}

function handle(problem: unknown, signal: AbortSignal, show: (text: string) => void, expire: () => void) {
  if (signal.aborted) return;
  if (problem instanceof ApiError && problem.status === 401) expire();
  else show(describe(problem));
}
