import { useEffect, useRef, useState } from 'react';
import { api, ApiError, describe } from './api';

const sources = {
  hh: 'Вакансии HH', trudvsem: 'Работа России', esco: 'ESCO', github: 'GitHub Issues', trends: 'GitHub Trends',
  docs: 'Официальная документация', hn: 'Hacker News', openalex: 'Научные статьи', skills: 'Agent Skills',
} as const;
type Source = keyof typeof sources;
type Step = {
  status: string; code?: string; collected?: number; changed?: number; busy?: number;
  found?: number; accepted?: number;
  errors?: Record<string, string>;
  items?: { id: number; title: string; url: string; score: number }[];
};
type Run = { id: string; running: boolean; steps: Partial<Record<Source, Step>> };
type VacancyQuality = { checked_at: string | null; vacancies: number; employers: number; fresh: boolean;
  approved: boolean; current_config: boolean; source: string | null };
type EscoMatch = 'exact' | 'broader';
type EscoQuality = { enabled: boolean; target: string; match: EscoMatch; sample: null | {
  checked_at: string; version: string; title: string; url: string; skills: number;
  match: EscoMatch; current_config: boolean;
} };
type EscoDetail = {
  occupation: { description: string };
  skills: { uri: string; url: string; title: string; title_ru: string | null; relations: string[] }[];
};
type Quality = {
  hh: VacancyQuality; trudvsem?: VacancyQuality; esco?: EscoQuality;
  materials: Record<'github' | 'trends' | 'docs', Record<string, number>> &
    { openalex?: Record<string, number>; skills?: Record<string, number> };
  automatic_check_enabled: boolean;
};
type Status = { run: Run | null; quality: Quality };
const statuses: Record<string, string> = {
  queued: 'В очереди', running: 'Идёт сбор', ok: 'Готово', partial: 'Частичный результат',
  failed: 'Ошибка', busy: 'Источник уже обновляется', skipped: 'Пропущен', interrupted: 'Прервано',
};
const messages: Record<string, string> = {
  HH_NOT_CONFIGURED: 'Подключите API HeadHunter.',
  HH_TIMEOUT: 'Не удалось завершить обновление вакансий вовремя.',
  HH_EMPTY_SAMPLE: 'По запросу профиля не найдено подходящих вакансий.',
  TRUDVSEM_DISABLED: 'Сбор отключён в настройках сервера.',
  TRUDVSEM_TIMEOUT: 'API «Работа России» не ответило вовремя.',
  TRUDVSEM_UNAVAILABLE: 'API «Работа России» недоступно.',
  TRUDVSEM_RATE_LIMITED: 'Портал ограничил частоту запросов. Повтори сбор позже.',
  TRUDVSEM_INVALID_RESPONSE: 'API вернуло данные в неожиданном формате.',
  TRUDVSEM_EMPTY_SAMPLE: 'По названию и синонимам профессии не найдено подходящих вакансий с описанием.',
  TRUDVSEM_SUPERSEDED: 'Настройки профессии изменились во время сбора. Запусти его снова.',
  ESCO_DISABLED: 'Сбор ESCO отключён в настройках сервера.',
  ESCO_NOT_CONFIGURED: 'Укажите англоязычное название или URI профессии ESCO.',
  ESCO_NOT_FOUND: 'Профессия не найдена. Проверьте английское название или укажите URI.',
  ESCO_AMBIGUOUS: 'По названию найдено несколько профессий ESCO. Укажите URI.',
  ESCO_RATE_LIMITED: 'ESCO ограничила частоту запросов. Повторите сбор позже.',
  ESCO_TIMEOUT: 'ESCO не ответила вовремя.',
  ESCO_UNAVAILABLE: 'API ESCO недоступно.',
  ESCO_INVALID_RESPONSE: 'API ESCO вернуло данные в неожиданном формате.',
  ESCO_NO_SKILLS: 'В ответе ESCO нет связанных навыков.',
  ESCO_SUPERSEDED: 'Настройки ESCO изменились во время сбора. Запустите сбор заново.',
  ESCO_INVALID_URI: 'Укажите URI профессии вида http://data.europa.eu/esco/occupation/…',
  GITHUB_NOT_CONFIGURED: 'Задайте репозитории или темы в форме отдельного сбора ниже.',
  DOCS_NOT_CONFIGURED: 'Выберите разделы в настройках официальной документации.',
  HN_DISABLED: 'Сбор отключён в конфигурации сервера.',
  HN_NOT_CONFIGURED: 'Для профиля пока нет списка технологий или свежей подтверждённой выборки вакансий.',
  HN_UNAVAILABLE: 'Ленты Hacker News недоступны.',
  OPENALEX_NOT_CONFIGURED: 'На сервере нужен ключ OpenAlex.',
  OPENALEX_NO_TASKS: 'Для поиска нужна свежая одобренная выборка вакансий с рабочими задачами.',
  OPENALEX_AUTH: 'OpenAlex отклонил ключ или доступ к полному тексту.',
  OPENALEX_UPSTREAM: 'Один из сервисов проверки статьи недоступен.',
  OPENALEX_ANALYSIS_FAILED: 'Модель не завершила проверку выдержки.',
  OPENALEX_RESPONSE_TOO_LARGE: 'Ответ источника превысил допустимый размер.',
  SKILLS_NOT_CONFIGURED: 'Для пилота подключены профили QA и Python backend.',
  SKILLS_ANALYSIS_FAILED: 'Модель не завершила проверку одного из скиллов.',
  DISCOVERY_RATE_LIMIT: 'Источник ограничил число запросов.',
  DISCOVERY_TIMEOUT: 'Источник не ответил вовремя.',
  DISCOVERY_UNAVAILABLE: 'Источник недоступен или вернул неожиданный ответ.',
};
const retryable = new Set(['partial', 'failed', 'busy', 'interrupted']);

function qualityNote(source: Source, quality: Quality | null): string {
  if (!quality) return '';
  if (source === 'hh' || source === 'trudvsem') {
    const sample = source === 'hh' ? quality.hh : quality.trudvsem;
    if (!sample) return '';
    if (!sample.checked_at) return 'выборки пока нет';
    const date = new Date(sample.checked_at).toLocaleDateString('ru-RU', { timeZone: 'Europe/Moscow' });
    return `выборка от ${date} · вакансий: ${sample.vacancies}${sample.fresh ? '' : ' · требует обновления'}`;
  }
  if (source === 'esco') return quality.esco?.sample ? `навыков: ${quality.esco.sample.skills}` : '';
  const counters = source === 'openalex' ? quality.materials.openalex
    : source === 'skills' ? quality.materials.skills
    : source === 'hn' ? undefined : quality.materials[source];
  if (!counters) return '';
  if (source === 'github') return `допущено ${counters.accepted ?? 0} · ожидают проверки ${counters.automatic_check ?? 0}`;
  if (source === 'docs') return `допущено ${counters.accepted ?? 0} · ожидают рассмотрения ${(counters.new ?? 0) + (counters.review ?? 0)}`;
  if (source === 'trends') return `сигналов популярности: ${counters.observe ?? 0}`;
  if (source === 'openalex') return `допущено ${counters.accepted ?? 0} · кандидаты ${(counters.new ?? 0) + (counters.observe ?? 0)}`;
  return `допущено ${counters.accepted ?? 0} · наблюдение ${(counters.new ?? 0) + (counters.observe ?? 0)}`;
}

export function EscoPanel({ esco, path, busy, onCollect, onSaved, onSessionExpired }: {
  esco: EscoQuality; path: string; busy: boolean; onCollect: () => void;
  onSaved: () => void; onSessionExpired: () => void;
}) {
  const [draft, setDraft] = useState<{ target: string; match: EscoMatch } | null>(null);
  const [saving, setSaving] = useState(false);
  const [savingError, setSavingError] = useState('');
  const [opened, setOpened] = useState(false);
  const [detail, setDetail] = useState<EscoDetail | null>(null);
  const [detailError, setDetailError] = useState('');
  const sample = esco.sample;
  const mapping = draft ?? { target: esco.target, match: esco.match };
  const checkedAt = sample?.checked_at;

  useEffect(() => {
    if (!opened || !checkedAt) return;
    const controller = new AbortController();
    setDetail(null);
    setDetailError('');
    api<EscoDetail>(path, { signal: controller.signal }).then(result => {
      if (!controller.signal.aborted) setDetail(result);
    }).catch(problem => {
      if (controller.signal.aborted) return;
      if (problem instanceof ApiError && problem.status === 401) onSessionExpired();
      else setDetailError(describe(problem));
    });
    return () => controller.abort();
  }, [path, opened, checkedAt, onSessionExpired]);

  async function save() {
    setSaving(true);
    setSavingError('');
    try {
      await api<void>(path, {
        method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(mapping),
      });
      onSaved();
      setDraft(null);
    } catch (problem) {
      if (problem instanceof ApiError && problem.status === 401) onSessionExpired();
      else setSavingError(problem instanceof ApiError ? messages[problem.code] ?? describe(problem) : describe(problem));
    } finally {
      setSaving(false);
    }
  }

  return <section className="stack">
    <h3>Справочник профессий ESCO</h3>
    <form className="stack" onSubmit={event => { event.preventDefault(); void save(); }}>
      <label>Английское название или URI профессии ESCO
        <input value={mapping.target} maxLength={180} disabled={busy || saving}
          onChange={event => setDraft({ ...mapping, target: event.target.value })}/>
      </label>
      <label>Соответствие профилю Modeus
        <select value={mapping.match} disabled={busy || saving}
          onChange={event => setDraft({ ...mapping, match: event.target.value as EscoMatch })}>
          <option value="broader">Более широкая профессия</option>
          <option value="exact">Точное соответствие</option>
        </select>
      </label>
      <div className="actions">
        <button type="submit" className="secondary" disabled={busy || saving || !draft}>Сохранить соответствие</button>
        <button type="button" className="secondary" onClick={onCollect}
          disabled={busy || saving || !!draft || !esco.enabled || !esco.target}>Обновить ESCO</button>
      </div>
    </form>
    {savingError && <p role="alert">{savingError}</p>}
    {!esco.enabled && <p className="meta">{messages.ESCO_DISABLED}</p>}
    {sample && <>
      <p><a href={sample.url} target="_blank" rel="noreferrer">{sample.title}</a> · {sample.version} · {sample.skills} навыков
        {' · '}{new Date(sample.checked_at).toLocaleString('ru-RU', { timeZone: 'Europe/Moscow' })}
        {sample.match === 'broader' && ' · более широкая профессия'}
        {!sample.current_config && ' · данные по прежним настройкам'}</p>
      <details onToggle={event => setOpened(event.currentTarget.open)}>
        <summary>Описание и навыки ESCO</summary>
        {detailError && <p role="alert">{detailError}</p>}
        {opened && !detail && !detailError && <p role="status">Загрузка навыков…</p>}
        {detail && <>
          {detail.occupation.description && <p lang="en">{detail.occupation.description}</p>}
          <ul>{detail.skills.map(skill => <li key={skill.uri}>
            <a href={skill.url} target="_blank" rel="noreferrer" lang={skill.title_ru ? 'ru' : 'en'}>
              {skill.title_ru ?? skill.title}
            </a>{skill.title_ru && <> · <span lang="en">{skill.title}</span></>}
            {' · '}{skill.relations.map(relation => relation === 'essential' ? 'основной' : 'дополнительный').join(', ')}
          </li>)}</ul>
        </>}
      </details>
    </>}
  </section>;
}

export function useCollectionStatus(profession: string, onSessionExpired: () => void, onCompleted: () => void) {
  const [run, setRun] = useState<Run | null>(null);
  const [quality, setQuality] = useState<Quality | null>(null);
  const [loadError, setLoadError] = useState('');
  const [startError, setStartError] = useState('');
  const [starting, setStarting] = useState(false);
  const [qualityRevision, setQualityRevision] = useState(0);
  const request = useRef<AbortController | null>(null);
  const previousRun = useRef<Run | null>(null);
  const path = '/discovery/collection/' + encodeURIComponent(profession);

  useEffect(() => () => request.current?.abort(), []);

  useEffect(() => {
    if (starting || !profession) return;
    const controller = new AbortController();
    const { signal } = controller;
    let timer: number | undefined;
    async function poll() {
      let delay = 15000;
      try {
        const result = await api<Status>(path, { signal });
        if (signal.aborted) return;
        const previous = previousRun.current;
        if (previous?.running && result.run && (!result.run.running ||
            (Object.keys(sources) as Source[]).some(source =>
              previous.steps[source]?.collected === undefined && result.run?.steps[source]?.collected !== undefined))) {
          onCompleted();
        }
        previousRun.current = result.run;
        setRun(result.run);
        setQuality(result.quality);
        setLoadError('');
        if (result.run?.running) delay = 2500;
      } catch (problem) {
        if (signal.aborted) return;
        if (problem instanceof ApiError && problem.status === 401) {
          onSessionExpired();
          return;
        }
        setLoadError(describe(problem));
      }
      if (!signal.aborted) timer = window.setTimeout(poll, delay);
    }
    void poll();
    return () => { controller.abort(); window.clearTimeout(timer); };
  }, [path, starting, qualityRevision, onCompleted, onSessionExpired]);

  async function start(source?: Source, escoOnly = false) {
    if (request.current || run?.running || (source && !run)) return;
    const controller = new AbortController();
    request.current = controller;
    setStarting(true);
    setStartError('');
    try {
      const result = await api<Run>(escoOnly ? path + '/esco' : path, {
        method: 'POST', signal: controller.signal,
        ...(source && run ? {
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ run_id: run.id, source }),
        } : {}),
      });
      if (controller.signal.aborted) return;
      previousRun.current = result;
      setRun(result);
    } catch (problem) {
      if (controller.signal.aborted) return;
      if (problem instanceof ApiError && problem.status === 401) onSessionExpired();
      else setStartError(describe(problem));
    } finally {
      request.current = null;
      if (!controller.signal.aborted) setStarting(false);
    }
  }

  return { run, quality, starting, startError, loadError, start,
    refreshQuality: () => setQualityRevision(value => value + 1) };
}

export type CollectionStatus = ReturnType<typeof useCollectionStatus>;

export function CollectionOverview({ status }: { status: CollectionStatus }) {
  const { run, quality, starting, startError, loadError, start } = status;
  return <div className="panel stack">
    <div className="actions">
      <button type="button" disabled={starting || run?.running} onClick={() => void start()}>
        {starting || run?.running ? 'Сбор запущен' : 'Собрать данные по профессии'}
      </button>
    </div>
    {startError && <p role="alert">{startError}</p>}
    {loadError && <p role="alert">{loadError}</p>}
    {(run || quality) && <ul className="source-list" aria-label="Состояние источников">
      {(Object.keys(sources) as Source[]).map(source => {
        const step = run?.steps[source];
        const note = qualityNote(source, quality);
        if (!step && !note) return null;
        return <li key={source}><strong>{sources[source]}</strong>
          {step && <> · {statuses[step.status] ?? step.status}
            {step.found !== undefined && <> · кандидатов {step.found}</>}
            {step.collected !== undefined && <> · {source === 'openalex' ? 'проверено' :
              source === 'esco' ? 'навыков' : 'собрано'} {step.collected}</>}
            {step.accepted !== undefined && <> · допущено {step.accepted}</>}
            {step.changed !== undefined && <> · новых версий {step.changed}</>}
            {!!step.busy && <> · уже обновляется {step.busy}</>}
            {step.code && <> · {messages[step.code] ?? step.code}</>}
          </>}
          {note && <> · {note}</>}
          {step && !run?.running && retryable.has(step.status) &&
            <button className="secondary retry-step" type="button" disabled={starting}
              onClick={() => void start(source)}>Повторить</button>}
          {step?.errors && Object.entries(step.errors).map(([target, error]) => <p key={target}>{error}</p>)}
          {!!step?.items?.length && <details>
            <summary>Кандидаты из последнего сбора</summary>
            <ul>{step.items.map(item => <li key={item.id}>
              <a href={item.url} target="_blank" rel="noreferrer">{item.title}</a> · рейтинг {item.score}
            </li>)}</ul>
          </details>}
        </li>;
      })}
    </ul>}
  </div>;
}
