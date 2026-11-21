import { useEffect, useRef, useState, type FormEvent } from 'react';
import { api, ApiError, commandKeys, describe, type Interview, type Page, type ProjectContext } from './api';

const statusLabels = {
  active: 'В процессе',
  completing: 'Собирается карточка',
  completed: 'Завершено',
};

const dateFormat = new Intl.DateTimeFormat('ru', { dateStyle: 'medium', timeStyle: 'short' });
const previewCount = 6;
const projectFields: [keyof ProjectContext, string][] = [
  ['task', 'Задача проекта'], ['period', 'Период'], ['role', 'Ваша роль в проекте'],
  ['responsibility', 'За что вы отвечали'], ['stack', 'Стек'], ['constraints', 'Ограничения'],
];

export function InterviewHome({ onOpen, onSessionExpired }: {
  onOpen: (id: string) => void; onSessionExpired: () => void;
}) {
  const [page, setPage] = useState<Page<Interview>>();
  const [loadError, setLoadError] = useState('');
  const [actionError, setActionError] = useState('');
  const [attempt, retry] = useState(0);
  const [projectCount, setProjectCount] = useState(1);
  const [historyExpanded, setHistoryExpanded] = useState(false);
  const [busy, setBusy] = useState<'create' | 'more'>();
  const execute = useRef(commandKeys());
  const lock = useRef(false);

  useEffect(() => {
    const controller = new AbortController();
    setLoadError('');
    const options = { signal: controller.signal };
    api<Page<Interview>>('/interviews', options)
      .then(interviews => {
        if (controller.signal.aborted) return;
        setPage(interviews);
      })
      .catch(error => {
        if (controller.signal.aborted) return;
        if (error instanceof ApiError && error.status === 401) onSessionExpired();
        else setLoadError(describe(error));
      });
    return () => controller.abort();
  }, [attempt, onSessionExpired]);

  async function run(action: 'create' | 'more', task: () => Promise<void>) {
    if (lock.current) return;
    lock.current = true;
    setBusy(action);
    setActionError('');
    try { await task(); }
    catch (error) {
      if (error instanceof ApiError && error.status === 401) onSessionExpired();
      else setActionError(describe(error));
    } finally {
      setBusy(undefined);
      lock.current = false;
    }
  }

  function create(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const value = (field: string) => String(form.get(field) ?? '').trim();
    if (!value('role')) {
      setActionError('Укажите вашу роль.');
      return;
    }
    const projects: ProjectContext[] = [];
    for (let index = 0; index < projectCount; index++) {
      const field = (name: keyof ProjectContext) => value(`project-${index}-${name}`);
      const project = { task: field('task'), period: field('period'), role: field('role'),
        responsibility: field('responsibility'), stack: field('stack'), constraints: field('constraints') };
      if (!Object.values(project).some(Boolean)) continue;
      if (!project.task) {
        setActionError(`Укажите задачу проекта ${index + 1}.`);
        return;
      }
      projects.push(project);
    }
    void run('create', async () => {
      const created = await execute.current<Interview>('/interviews', {
        context: { role: value('role'), experience: value('experience'),
          responsibilities: value('responsibilities'), conditions: value('conditions'), projects },
      });
      onOpen(created.id);
    });
  }

  function loadMore() {
    if (!page?.next_cursor) return;
    const cursor = page.next_cursor;
    void run('more', async () => {
      const result = await api<Page<Interview>>(`/interviews?cursor=${encodeURIComponent(cursor)}`);
      setPage({ items: [...page.items, ...result.items], next_cursor: result.next_cursor });
    });
  }

  if (loadError) return <section className="panel">
    <p role="alert">{loadError}</p>
    <button onClick={() => retry(n => n + 1)}>Повторить загрузку</button>
  </section>;
  if (!page) return <p role="status">Загружаем интервью…</p>;

  const sorted = [...page.items].sort((a, b) => b.updated_at.localeCompare(a.updated_at));
  const visible = historyExpanded ? sorted : sorted.slice(0, previewCount);
  const showHistoryToggle = historyExpanded || sorted.length > previewCount || !!page.next_cursor;

  return <div className="workspace">
    <form className="panel" onSubmit={create} aria-labelledby="new-title">
      <h2 id="new-title">Новое интервью</h2>
      <p>Расскажите о рабочем случае своими словами. Тема появится из вашего рассказа.</p>
      <fieldset disabled={!!busy}>
        <label htmlFor="role">Ваша роль</label>
        <input id="role" name="role" placeholder="Например, Python backend-разработчик" required maxLength={200}/>
        <label htmlFor="experience">Опыт <span>необязательно</span></label>
        <input id="experience" name="experience" maxLength={1000}/>
        <label htmlFor="responsibilities">За что вы отвечаете <span>необязательно</span></label>
        <textarea id="responsibilities" name="responsibilities" rows={2} maxLength={1000}/>
        <label htmlFor="conditions">В каком контексте вы работаете <span>необязательно</span></label>
        <textarea id="conditions" name="conditions" rows={2} maxLength={1000}/>
        <details className="interview-projects">
          <summary>Проекты <span>необязательно</span></summary>
          {Array.from({ length: projectCount }, (_, index) =>
            <fieldset key={index}>
              <legend>Проект {index + 1}</legend>
              {projectFields.map(([name, label]) => {
                const id = `project-${index}-${name}`;
                return <div key={name}><label htmlFor={id}>{label}</label><input id={id} name={id} maxLength={300}/></div>;
              })}
            </fieldset>)}
          {projectCount < 3 && <button className="secondary" type="button" onClick={() => setProjectCount(n => n + 1)}>
            Добавить проект
          </button>}
        </details>
        <button>{busy === 'create' ? 'Создаём…' : 'Начать интервью'}</button>
      </fieldset>
    </form>
    <section className="panel saved" aria-labelledby="saved-title">
      <h2 id="saved-title">Мои интервью</h2>
      {sorted.length === 0 ? <p>Сохранённых интервью пока нет.</p>
        : <ul>{visible.map(session =>
          <li key={session.id}><a href={`#/interviews/${session.id}`}>
            <strong>{session.current_topic_title ?? 'Интервью'}</strong>
            <span>{session.context.role} · {statusLabels[session.status]}</span>
            <time dateTime={session.updated_at}>{dateFormat.format(new Date(session.updated_at))}</time>
          </a></li>)}</ul>}
      {historyExpanded && page.next_cursor && <button className="secondary" disabled={!!busy} onClick={loadMore}>
        {busy === 'more' ? 'Загружаем…' : 'Загрузить ещё'}
      </button>}
      {showHistoryToggle && <button className={historyExpanded ? 'quiet' : 'secondary'} aria-expanded={historyExpanded}
        onClick={() => setHistoryExpanded(current => !current)}>
        {historyExpanded ? 'Скрыть' : 'Посмотреть больше'}
      </button>}
    </section>
    {actionError && <p className="workspace-error" role="alert">{actionError}</p>}
  </div>;
}
