import { useEffect, useRef, useState } from 'react';
import { api, ApiError, describe } from './api';

const sources = {
  hh: 'Вакансии HH', github: 'GitHub Issues', trends: 'GitHub Trends',
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
type Quality = {
  hh: { checked_at: string | null; vacancies: number; employers: number; fresh: boolean; approved: boolean;
    current_config: boolean; source: string | null };
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
  GITHUB_NOT_CONFIGURED: 'Задайте репозитории или темы в форме отдельного сбора ниже.',
  DOCS_NOT_CONFIGURED: 'Выберите разделы в настройках официальной документации.',
  HN_DISABLED: 'Сбор отключён в конфигурации сервера.',
  HN_NOT_CONFIGURED: 'Для профиля пока нет списка технологий или свежей одобренной выборки HH.',
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

function QualitySummary({ quality }: { quality: Quality }) {
  const { hh, materials } = quality;
  const { github, docs, trends, openalex: research, skills } = materials;
  const hhDate = hh.checked_at && new Date(hh.checked_at).toLocaleDateString('ru-RU', { timeZone: 'Europe/Moscow' });
  return <ul>
    <li><a href="#/data/vacancies">Вакансии HH</a>{hhDate ? <>
      {' · выборка от '}{hhDate} · {hh.vacancies} вакансий, {hh.employers} работодателей
      {' · '}{hh.fresh ? 'свежая' : 'срок актуальности истёк'}
      {!(hh.approved && hh.current_config) && ' · профиль или выборка требуют проверки'}
      {hh.source === 'file' && ' · импорт из файла'}
    </> : ' · выборки пока нет'}</li>
    <li>GitHub Issues · допущено {github.accepted ?? 0} · ожидают автопроверки {github.automatic_check ?? 0}
      {(github.automatic_check ?? 0) > 0 && !quality.automatic_check_enabled && ' · модель не настроена'}
      {' · наблюдение, отклонено или просрочено '}
      {(github.observe ?? 0) + (github.rejected ?? 0) + (github.expired ?? 0)}</li>
    <li>Документация · допущено {docs.accepted ?? 0} · ожидают рассмотрения {(docs.new ?? 0) + (docs.review ?? 0)}
      {' · наблюдение, отклонено, недоступно или просрочено '}
      {(docs.observe ?? 0) + (docs.rejected ?? 0) + (docs.unavailable ?? 0) + (docs.expired ?? 0)}</li>
    <li>GitHub Trends · {trends.observe ?? 0} сигналов популярности. Hacker News · кандидаты по технологиям и активности обсуждения.</li>
    {research && <li>Научные статьи · допущено {research.accepted ?? 0} · кандидаты {(research.new ?? 0) + (research.observe ?? 0)}
      {' · отклонено или просрочено '}{(research.rejected ?? 0) + (research.expired ?? 0)}</li>}
    {skills && <li>Agent Skills · допущено {skills.accepted ?? 0} · наблюдение {(skills.new ?? 0) + (skills.observe ?? 0)}
      {' · отклонено или устарело '}{(skills.rejected ?? 0) + (skills.expired ?? 0)}</li>}
  </ul>;
}

export function ProfessionCollection({ profession, onSessionExpired, onCompleted }: {
  profession: string; onSessionExpired: () => void; onCompleted: () => void;
}) {
  const [run, setRun] = useState<Run | null>(null);
  const [quality, setQuality] = useState<Quality | null>(null);
  const [loadError, setLoadError] = useState('');
  const [startError, setStartError] = useState('');
  const [starting, setStarting] = useState(false);
  const request = useRef<AbortController | null>(null);
  const previousRun = useRef<Run | null>(null);
  const path = '/discovery/collection/' + encodeURIComponent(profession);

  useEffect(() => () => request.current?.abort(), []);

  useEffect(() => {
    if (starting) return;
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
  }, [path, starting, onCompleted, onSessionExpired]);

  async function start(source?: Source) {
    if (request.current || run?.running || (source && !run)) return;
    const controller = new AbortController();
    request.current = controller;
    setStarting(true);
    setStartError('');
    try {
      const result = await api<Run>(path, {
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

  return <div className="panel stack">
    <div className="actions">
      <button type="button" disabled={starting || run?.running} onClick={() => void start()}>
        {starting || run?.running ? 'Сбор запущен' : 'Собрать данные по профессии'}
      </button>
    </div>
    {startError && <p role="alert">{startError}</p>}
    {loadError && <p role="alert">{loadError}</p>}
    {run && <ul aria-label="Результаты сбора">
      {(Object.keys(sources) as Source[]).map(source => {
        const step = run.steps[source];
        if (!step) return null;
        return <li key={source}><strong>{sources[source]}</strong> · {statuses[step.status] ?? step.status}
          {step.found !== undefined && <> · кандидатов {step.found}</>}
          {step.collected !== undefined && <> · {source === 'openalex' ? 'проверено' : 'собрано или обновлено'} {step.collected}</>}
          {step.accepted !== undefined && <> · допущено к интервью {step.accepted}</>}
          {step.changed !== undefined && <> · новых или изменённых версий {step.changed}</>}
          {!!step.busy && <> · уже обновляется {step.busy}</>}
          {step.code && <> · {messages[step.code] ?? step.code}</>}
          {!run.running && retryable.has(step.status) &&
            <> <button className="secondary" type="button" disabled={starting}
              onClick={() => void start(source)}>Повторить {sources[source]}</button></>}
          {step.errors && Object.entries(step.errors).map(([target, error]) => <p key={target}>{error}</p>)}
          {!!step.items?.length && <details>
            <summary>Кандидаты из последнего сбора</summary>
            <ul>{step.items.map(item => <li key={item.id}>
              <a href={item.url} target="_blank" rel="noreferrer">{item.title}</a> · рейтинг {item.score}
            </li>)}</ul>
          </details>}
        </li>;
      })}
    </ul>}
    <h2>Качество материалов</h2>
    {quality && <QualitySummary quality={quality}/>}
    <p className="meta">Для интервью проверяются цитата, задача и применимость материала. Популярность
      в GitHub Trends и Hacker News используется для поиска кандидатов.</p>
  </div>;
}
