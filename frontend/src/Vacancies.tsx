import { useEffect, useState, type FormEvent } from 'react';
import { api, ApiError, describe } from './api';

const experiences = {
  noExperience: 'Без опыта', between1And3: '1–3 года', between3And6: '3–6 лет', moreThan6: 'Более 6 лет',
};
const levels = { junior: 'Junior', middle: 'Middle', senior: 'Senior', lead: 'Lead' };
const sources = { hh: 'HH', trudvsem: 'Работа России', file: 'Импорт из файла' };
const errors: Record<string, string> = {
  HH_NOT_CONFIGURED: 'Укажи параметры HH в .env и перезапусти сервер.',
  HH_ALREADY_RUNNING: 'Для этого профиля уже идёт сбор.',
  HH_TOKEN_FAILED: 'HH отказал в выдаче OAuth-токена. Проверь доступ приложения в HH.',
  HH_AUTH_FAILED: 'HH отклонил токен при запросе вакансий.',
  HH_RATE_LIMITED: 'HH ограничил частоту запросов. Повтори сбор позже.',
  HH_UNAVAILABLE: 'HH не ответил после повторных попыток.',
  HH_CANCELLED: 'Сбор остановлен.',
  HH_INVALID_RESPONSE: 'HH вернул данные в неожиданном формате.',
  HH_REQUEST_REJECTED: 'HH отклонил запрос вакансий.',
  HH_EMPTY_SAMPLE: 'Подходящих вакансий нет. Проверь регионы сбора.',
  HH_TIMEOUT: 'Сбор прервался по таймауту.',
  HH_INVALID_AREAS: 'Проверь идентификаторы регионов HH в .env.',
  PROFILE_EXISTS: 'Профиль с таким названием уже есть.',
  PROFILE_INVALID_TITLE: 'Укажи название профессии.',
  PROFILE_REVIEW_NOT_READY: 'Для включения нужна свежая выборка по текущим настройкам.',
};

type Vacancy = {
  id: string; title: string; url: string; tasks: string[]; requirements?: string[]; conditions: string[]; skills: string[];
  source: keyof typeof sources;
  level: keyof typeof levels | null; experience_band: keyof typeof experiences | null;
  evidence: { field_path: string; source_field: string; quote: string }[];
};
const stages = {
  queued: 'Запускаем сбор', token: 'Получаем OAuth-токен', search: 'Запрашиваем список вакансий',
  details: 'Проверяем описания', file: 'Читаем файл', saving: 'Сохраняем выборку',
};
type Profile = {
  id: string; title: string; running: boolean; error: string | null;
  search_query: string; aliases: string[] | null; approved: boolean; review_ready: boolean;
  started_at: string | null; updated_at: string | null; stale: boolean;
  progress: { stage: keyof typeof stages; target: number; accepted: number; details_fetched: number;
    details_reused: number; reported_at: string } | null;
  stats: { vacancies?: number; employers?: number; source?: 'hh' | 'file'; details_fetched?: number; details_reused?: number };
  trudvsem?: { updated_at: string; vacancies: number; employers: number; stale: boolean; current_config: boolean } | null;
};
type Overview = { configured: boolean; profiles: Profile[] };
type Listings = { cards: Vacancy[]; total: number };
type Candidate = { role: string; interviews: number };
type ProfileDraft = { id?: string; title: string; search_query: string; aliases: string };

const dateFormat = new Intl.DateTimeFormat('ru-RU', { dateStyle: 'short', timeStyle: 'short' });
const timeFormat = new Intl.DateTimeFormat('ru-RU', { timeStyle: 'medium' });

function Filter({ id, label, value, onChange, choices }: {
  id: string; label: string; value: string; onChange: (value: string) => void; choices: Record<string, string>;
}) {
  return <div><label htmlFor={id}>{label}</label>
    <select id={id} value={value} onChange={event => onChange(event.target.value)}>
      <option value="">Все</option>
      {Object.entries(choices).map(([key, title]) => <option key={key} value={key}>{title}</option>)}
    </select>
  </div>;
}

export function Vacancies({ profile, collectionRefresh, onProfileSaved, onSessionExpired }: {
  profile: string; collectionRefresh: number; onProfileSaved: (profile: { id: string; title: string }) => void;
  onSessionExpired: () => void;
}) {
  const [experience, setExperience] = useState('');
  const [level, setLevel] = useState('');
  const [source, setSource] = useState('');
  const [overview, setOverview] = useState<Overview>();
  const [listings, setListings] = useState<{ key: string; data: Listings }>();
  const [refresh, setRefresh] = useState(0);
  const [busy, setBusy] = useState(false);
  const [connectionError, setConnectionError] = useState('');
  const [listError, setListError] = useState('');
  const [actionError, setActionError] = useState('');
  const [draft, setDraft] = useState<ProfileDraft>();
  const [candidates, setCandidates] = useState<Candidate[]>();
  const [candidateError, setCandidateError] = useState('');
  const selected = overview?.profiles.find(item => item.id === profile);
  const hasSample = !!(selected?.updated_at || selected?.trudvsem?.updated_at);
  const revision = `${selected?.updated_at ?? ''}/${selected?.trudvsem?.updated_at ?? ''}`;
  const lastProgress = selected?.progress?.reported_at ?? selected?.started_at;
  const waitingForProgress = !!lastProgress && Date.now() - Date.parse(lastProgress) > 60_000;
  const params = new URLSearchParams({ profession_id: profile });
  if (experience) params.set('experience_band', experience);
  if (level) params.set('level', level);
  if (source) params.set('source', source);
  const query = params.toString();
  const collection = listings?.key === query ? listings.data : undefined;

  useEffect(() => {
    const controller = new AbortController();
    let timer: number | undefined;
    async function load() {
      try {
        const data = await api<Overview>('/vacancies/overview', { signal: controller.signal });
        if (controller.signal.aborted) return;
        setOverview(data);
        setConnectionError('');
        timer = window.setTimeout(load, data.profiles.some(item => item.running) ? 2000 : 10000);
      } catch (error) {
        if (controller.signal.aborted) return;
        if (error instanceof ApiError && error.status === 401) onSessionExpired();
        else if (error instanceof ApiError && error.status === 404) {
          setConnectionError('Backend запущен без маршрута обзора вакансий. Перезапусти backend с актуальным кодом.');
          timer = window.setTimeout(load, 10000);
        }
        else {
          setConnectionError('Статус сбора недоступен. Повторяем подключение…');
          timer = window.setTimeout(load, 3000);
        }
      }
    }
    void load();
    return () => { controller.abort(); window.clearTimeout(timer); };
  }, [refresh, collectionRefresh, onSessionExpired]);

  useEffect(() => {
    if (!hasSample) return;
    const controller = new AbortController();
    let timer: number | undefined;
    setListError('');
    async function load() {
      try {
        const data = await api<Listings>(`/vacancies?${query}`, { signal: controller.signal });
        if (controller.signal.aborted) return;
        setListings({ key: query, data });
        setListError('');
      } catch (error) {
        if (controller.signal.aborted) return;
        if (error instanceof ApiError && error.status === 401) onSessionExpired();
        else {
          setListError('Список вакансий недоступен. Повторяем загрузку…');
          timer = window.setTimeout(load, 3000);
        }
      }
    }
    void load();
    return () => { controller.abort(); window.clearTimeout(timer); };
  }, [query, hasSample, revision, refresh, onSessionExpired]);

  async function collect() {
    setBusy(true);
    setActionError('');
    try {
      await api(`/vacancies/${encodeURIComponent(profile)}/collect`, { method: 'POST' });
      setRefresh(value => value + 1);
    } catch (error) {
      if (error instanceof ApiError && error.status === 401) onSessionExpired();
      else if (error instanceof ApiError) setActionError(errors[error.code] ?? describe(error));
      else setActionError(describe(error));
    } finally {
      setBusy(false);
    }
  }

  async function loadCandidates() {
    try {
      setCandidateError('');
      const result = await api<{ candidates: Candidate[] }>('/vacancies/candidates');
      setCandidates(result.candidates);
    } catch (error) {
      if (error instanceof ApiError && error.status === 401) onSessionExpired();
      else setCandidateError(describe(error));
    }
  }

  async function saveProfile(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!draft) return;
    const aliases = draft.aliases.split('\n').map(value => value.trim()).filter(Boolean);
    if (aliases.length > 16 || aliases.some(value => value.length > 120)) {
      setActionError('Можно указать до 16 синонимов длиной до 120 символов каждый.');
      return;
    }
    setBusy(true);
    setActionError('');
    try {
      const base = '/vacancies/profiles';
      const result = await api<{ id: string }>(draft.id ? `${base}/${draft.id}` : base, {
        method: draft.id ? 'PUT' : 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ title: draft.title, search_query: draft.search_query, aliases }),
      });
      setDraft(undefined);
      onProfileSaved({ id: result.id, title: draft.title.trim() });
      setRefresh(value => value + 1);
      if (candidates) void loadCandidates();
    } catch (error) {
      if (error instanceof ApiError && error.status === 401) onSessionExpired();
      else setActionError(error instanceof ApiError ? errors[error.code] ?? describe(error) : describe(error));
    } finally { setBusy(false); }
  }

  async function reviewProfile(approved: boolean) {
    setBusy(true);
    setActionError('');
    try {
      await api(`/vacancies/profiles/${encodeURIComponent(profile)}/review`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ approved }),
      });
      setRefresh(value => value + 1);
    } catch (error) {
      if (error instanceof ApiError && error.status === 401) onSessionExpired();
      else setActionError(error instanceof ApiError ? errors[error.code] ?? describe(error) : describe(error));
    } finally { setBusy(false); }
  }

  function newProfile(role = '') {
    setActionError('');
    setDraft({ title: role, search_query: role, aliases: '' });
  }

  return <div className="stack">
    {connectionError && <p role="alert">{connectionError} {overview && 'Показаны последние полученные данные.'}</p>}
    {overview ? <section className="panel" aria-label="Управление профессиями">
      <div className="actions">
        <button type="button" disabled={busy} onClick={() => newProfile()}>Добавить профессию</button>
        <button type="button" className="secondary" disabled={busy} onClick={() => void loadCandidates()}>Роли из интервью</button>
      </div>
      {candidateError && <p role="alert">{candidateError}</p>}
      {candidates && (candidates.length ? <ul aria-label="Новые роли из интервью">{candidates.map(item => <li key={item.role}>
        <button className="quiet" type="button" disabled={busy} onClick={() => newProfile(item.role)}>
          {item.role} · Интервью: {item.interviews}
        </button>
      </li>)}</ul> : <p>Новых ролей пока нет.</p>)}
    </section> : !connectionError && <p role="status">Загружаем профили…</p>}
    {actionError && <p role="alert">{actionError}</p>}
    {draft && <form className="panel" onSubmit={saveProfile}>
      <h2>{draft.id ? 'Настройки профессии' : 'Новая профессия'}</h2>
      <fieldset disabled={busy}>
        <label htmlFor="profession-title">Название профессии</label>
        <input id="profession-title" required maxLength={120} value={draft.title}
          onChange={event => setDraft({ ...draft, title: event.target.value })}/>
        <label htmlFor="profession-query">Поисковый запрос HH</label>
        <input id="profession-query" required maxLength={500} value={draft.search_query}
          onChange={event => setDraft({ ...draft, search_query: event.target.value })}/>
        <label htmlFor="profession-aliases">Синонимы роли и названия вакансии <span>по одному на строку</span></label>
        <textarea id="profession-aliases" rows={3} maxLength={1936} value={draft.aliases}
          onChange={event => setDraft({ ...draft, aliases: event.target.value })}/>
        {draft.id && <p>После сохранения собери новую выборку и подтверди её для интервью.</p>}
        <div className="actions"><button>Сохранить профиль</button>
          <button className="secondary" type="button" onClick={() => { setDraft(undefined); setActionError(''); }}>Отмена</button></div>
      </fieldset>
    </form>}
    {selected && overview && <>
      <section className="panel" aria-label={`Выборка ${selected.title}`}>
        <h2>{selected.title}</h2>
        <p className="meta">{selected.approved ? 'Включён для интервью' : 'Требует проверки редактора'}</p>
        <div className="vacancy-filters">
          <Filter id="market-experience" label="Опыт" choices={experiences} value={experience} onChange={setExperience}/>
          <Filter id="market-level" label="Уровень" choices={levels} value={level} onChange={setLevel}/>
          <Filter id="market-source" label="Источник" choices={sources} value={source} onChange={setSource}/>
        </div>
        {selected.updated_at && <p>{selected.stats.source === 'file' ? 'Импорт из файла' : 'HH'} · Выборка от {dateFormat.format(new Date(selected.updated_at))}. {' '}
          Работодателей: {selected.stats.employers ?? 0}.
          {selected.stats.details_fetched !== undefined && ` Описаний загружено: ${selected.stats.details_fetched}. Из кэша: ${selected.stats.details_reused ?? 0}.`}</p>}
        {selected.stale && <p>Срок актуальности выборки {selected.stats.source === 'file' ? 'из файла' : 'HH'} истёк.</p>}
        {selected.trudvsem && <p>Работа России · Выборка от {dateFormat.format(new Date(selected.trudvsem.updated_at))}. {' '}
          Вакансий: {selected.trudvsem.vacancies}. Работодателей: {selected.trudvsem.employers}.
          {selected.trudvsem.stale && ' Срок актуальности истёк.'}
          {!selected.trudvsem.current_config && ' Выборка собрана по предыдущим настройкам.'}</p>}
        {selected.running && <div className="market-progress" role="status">
          <span className="market-spinner" aria-hidden="true"/>
          <div><strong>{stages[selected.progress?.stage ?? 'queued']}</strong>
            {selected.progress && <div>Подходят {selected.progress.accepted} из {selected.progress.target} · Загружено описаний {selected.progress.details_fetched} · Из кэша {selected.progress.details_reused}</div>}
            {selected.progress && <small>Последний сигнал {timeFormat.format(new Date(selected.progress.reported_at))}</small>}
            {waitingForProgress && <div>Больше минуты без новых данных о ходе сбора.</div>}
          </div>
        </div>}
        {selected.error && <p role="alert">{errors[selected.error] ?? 'Не удалось обновить выборку HH.'}
          {selected.updated_at && ' Показана последняя успешная выборка.'}</p>}
        {!overview.configured && <p>{errors.HH_NOT_CONFIGURED}</p>}
        {!selected.approved && !selected.review_ready && <p>Для подключения к интервью собери свежую выборку по текущим настройкам.</p>}
        <div className="actions">
          <button disabled={busy || selected.running || !!connectionError || !overview.configured} onClick={() => void collect()}>
            {selected.approved ? 'Обновить вакансии HH' : 'Собрать пробную выборку HH'}
          </button>
          <button className="secondary" type="button" disabled={busy || selected.running} onClick={() => {
            setActionError('');
            setDraft({ id: selected.id, title: selected.title, search_query: selected.search_query, aliases: selected.aliases?.join('\n') ?? '' });
          }}>Настроить профиль</button>
          <button className="secondary" type="button" disabled={busy || (!selected.approved && !selected.review_ready)}
            onClick={() => void reviewProfile(!selected.approved)}>
            {selected.approved ? 'Отключить в интервью' : 'Подтвердить для интервью'}
          </button>
        </div>
      </section>
      {listError && <p role="alert">{listError}</p>}
      {!hasSample && <p>{selected.running ? 'Первая выборка появится после завершения сбора.'
        : selected.error ? 'Сохранённой выборки пока нет.' : 'Сбор для этого профиля ещё не запускался.'}</p>}
      {hasSample && !collection && !listError && <p role="status">Загружаем вакансии…</p>}
      {hasSample && collection && <p className="market-muted">Показано {collection.cards.length} из {collection.total} вакансий</p>}
      {hasSample && collection?.cards.length === 0 && <p>По текущим фильтрам совпадений нет.</p>}
      {hasSample && collection?.cards.map(card => <article className="panel" key={card.id}>
        <h2><a className="card-link" href={card.url} target="_blank" rel="noreferrer">{card.title}</a></h2>
        <p className="meta">{sources[card.source]}</p>
        {(card.experience_band || card.level) && <p>{[card.experience_band && experiences[card.experience_band],
          card.level && levels[card.level]].filter(Boolean).join(' · ')}</p>}
        {card.tasks.length > 0 && <div><strong>Рабочие задачи</strong><ul>{card.tasks.map((task, index) => {
          const source = card.evidence.find(item => item.field_path === `tasks.${index}` && item.quote === task);
          return <li key={task}>«{task}» {source && <a className="market-muted" href={card.url} target="_blank" rel="noreferrer">
            {card.source === 'trudvsem' ? 'Описание на портале' :
              source.source_field === 'description' ? 'Описание на HH' : 'Фрагмент из выдачи HH'}
          </a>}</li>;
        })}</ul></div>}
        {!!card.requirements?.length && <div><strong>Требования к кандидату</strong>
          <ul>{card.requirements.map(requirement => <li key={requirement}>{requirement}</li>)}</ul></div>}
        {card.skills.length > 0 && <p><strong>Навыки и инструменты</strong> · {card.skills.join(', ')}</p>}
        {card.conditions.length > 0 && <div><strong>Условия работы</strong>
          <ul>{card.conditions.map(condition => <li key={condition}>{condition}</li>)}</ul></div>}
        {card.evidence.length > 0 && <details><summary>Основания</summary><ul>
          {card.evidence.map(item => <li key={JSON.stringify([item.field_path, item.source_field, item.quote])}>
            {item.quote} <small>({item.source_field})</small>
          </li>)}
        </ul></details>}
      </article>)}
    </>}
  </div>;
}
