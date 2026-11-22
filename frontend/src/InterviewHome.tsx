import { useEffect, useRef, useState, type FormEvent } from 'react';
import {
  api, ApiError, command, commandKeys, describe, tones, type Context, type Interview, type Page, type ProjectContext, type Tone,
} from './api';

const statusLabels = {
  active: 'В процессе',
  completing: 'Собирается карточка',
  completed: 'Завершено',
};

const dateFormat = new Intl.DateTimeFormat('ru', { dateStyle: 'medium', timeStyle: 'short' });
const previewCount = 5;
const projectFields: [keyof ProjectContext, string, string, number][] = [
  ['task', 'Что нужно было сделать в проекте?', 'Например, перенести оплату на нового провайдера', 1000],
  ['period', 'Когда вы над ним работали?', 'Например, с мая по сентябрь 2025', 300],
  ['role', 'Какая у вас была роль?', 'Например, разработчик в команде платежей', 300],
  ['responsibility', 'Что именно вы делали сами?', 'Например, подключили API и настроили повтор платежа', 1000],
  ['stack', 'Какие инструменты использовали?', 'Например, Python, FastAPI, PostgreSQL', 300],
  ['constraints', 'Что усложняло работу?', 'Например, нельзя было останавливать приём платежей', 1000],
];
const emptyProject: ProjectContext = { task: '', period: '', role: '', responsibility: '', stack: '', constraints: '' };
type Profession = { id: string; title: string };
type InterviewProfessions = { items: Profession[]; selected_id: string | null };

function ProfileForm({ profile, catalog, busy, onSave, onCancel }: {
  profile: Context | null; catalog: InterviewProfessions; busy: boolean;
  onSave: (context: Context) => void; onCancel: () => void;
}) {
  const [projects, setProjects] = useState(profile?.projects ?? []);
  const [error, setError] = useState('');
  const selectedId = catalog.items.find(item => item.title === profile?.role)?.id ?? catalog.selected_id ?? '';

  function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const read = (field: string) => String(form.get(field) ?? '').trim();
    const role = catalog.items.find(item => item.id === read('profession'))?.title;
    if (!role) return setError('Выберите профессию из списка.');
    const filled = projects
      .map(project => ({
        task: project.task.trim(), period: project.period.trim(), role: project.role.trim(),
        responsibility: project.responsibility.trim(), stack: project.stack.trim(), constraints: project.constraints.trim(),
      }))
      .filter(project => Object.values(project).some(Boolean));
    if (filled.some(project => !project.task)) return setError('Опишите задачу каждого добавленного проекта.');
    setError('');
    onSave({ role, experience: read('experience'), responsibilities: read('responsibilities'),
      conditions: read('conditions'), projects: filled, tone: (read('tone') || 'friendly') as Tone });
  }

  return <form onSubmit={save} className="interview-profile-form">
    <fieldset disabled={busy}>
      <div className="interview-profile-basics">
        <div>
          <label htmlFor="profile-role">По какой профессии хотите пройти интервью?</label>
          <select id="profile-role" name="profession" defaultValue={selectedId} required
            disabled={!catalog.items.length}>
            <option value="" disabled>{catalog.items.length ? 'Выберите профессию' : 'Список профессий пуст'}</option>
            {catalog.items.map(item => <option key={item.id} value={item.id}>{item.title}</option>)}
          </select>
          {profile && !selectedId && catalog.items.length > 0 &&
            <p className="meta">В анкете указано «{profile.role}». Выберите подходящую профессию из списка.</p>}
        </div>
        <div>
          <label htmlFor="profile-experience">Как давно вы этим занимаетесь? <span>Необязательно</span></label>
          <textarea id="profile-experience" name="experience" defaultValue={profile?.experience} maxLength={1000}
            rows={2} placeholder="Например, три года, последние полгода в команде платежей" />
        </div>
        <div>
          <label htmlFor="profile-responsibilities">Какими задачами вы обычно занимаетесь?</label>
          <textarea id="profile-responsibilities" name="responsibilities" defaultValue={profile?.responsibilities}
            maxLength={1000} rows={3} placeholder="Например, разрабатываю API и разбираю сбои в оплате" />
        </div>
        <div>
          <label htmlFor="profile-conditions">Что важно знать о вашей работе?</label>
          <textarea id="profile-conditions" name="conditions" defaultValue={profile?.conditions} maxLength={1000}
            rows={3} placeholder="Например, работаю в команде из пяти человек, сервис должен быть доступен круглосуточно" />
        </div>
        <div>
          <label htmlFor="profile-tone">Как интервьюеру с вами говорить?</label>
          <select id="profile-tone" name="tone" defaultValue={profile?.tone ?? 'friendly'}>
            {Object.entries(tones).map(([value, title]) => <option key={value} value={value}>{title}</option>)}
          </select>
        </div>
      </div>
      <details className="interview-projects" open={projects.length ? true : undefined}>
        <summary>Проекты <span>{projects.length ? `${projects.length} из 3` : 'Необязательно'}</span></summary>
        <p>Добавьте проекты, о которых вам было бы интересно рассказать.</p>
        {projects.map((project, index) =>
          <fieldset key={index}>
            <legend>Проект {index + 1}</legend>
            <div className="interview-project-fields">
              {projectFields.map(([field, label, placeholder, limit]) => {
                const id = `profile-project-${index}-${field}`;
                return <div key={field}>
                  <label htmlFor={id}>{label}</label>
                  <textarea id={id} value={project[field]} maxLength={limit} rows={2} placeholder={placeholder}
                    onChange={event => {
                      const value = event.target.value;
                      setProjects(current => current.map((item, position) =>
                        position === index ? { ...item, [field]: value } : item));
                    }} />
                </div>;
              })}
            </div>
            <button type="button" className="quiet" aria-label={`Убрать проект ${index + 1}`}
              onClick={() => setProjects(current => current.filter((_, position) => position !== index))}>Убрать проект</button>
          </fieldset>
        )}
        {projects.length < 3 && <button type="button" className="secondary"
          onClick={() => setProjects(current => [...current, emptyProject])}>Добавить проект</button>}
      </details>
      {error && <p role="alert">{error}</p>}
      <div className="actions">
        <button disabled={!catalog.items.length}>{busy ? 'Сохраняем…' : 'Сохранить анкету'}</button>
        {profile && <button type="button" className="quiet" onClick={onCancel}>Отмена</button>}
      </div>
    </fieldset>
  </form>;
}

export function InterviewHome({ onOpen, onSessionExpired }: {
  onOpen: (id: string) => void; onSessionExpired: () => void;
}) {
  const [page, setPage] = useState<Page<Interview>>();
  const [loadError, setLoadError] = useState('');
  const [actionError, setActionError] = useState('');
  const [attempt, retry] = useState(0);
  const [profile, setProfile] = useState<Context | null>(null);
  const [catalog, setCatalog] = useState<InterviewProfessions>();
  const [editing, setEditing] = useState(false);
  const [historyExpanded, setHistoryExpanded] = useState(false);
  const [busy, setBusy] = useState<'create' | 'more' | 'save'>();
  const execute = useRef(commandKeys());
  const lock = useRef(false);

  useEffect(() => {
    const controller = new AbortController();
    setLoadError('');
    const options = { signal: controller.signal };
    Promise.all([
      api<Page<Interview>>('/interviews', options),
      api<Context | null>('/interview-profile', options),
      api<InterviewProfessions>('/interview-professions', options),
    ])
      .then(([interviews, savedProfile, professions]) => {
        if (controller.signal.aborted) return;
        setPage(interviews);
        setProfile(savedProfile);
        setCatalog(professions);
      })
      .catch(error => {
        if (controller.signal.aborted) return;
        if (error instanceof ApiError && error.status === 401) onSessionExpired();
        else setLoadError(describe(error));
      });
    return () => controller.abort();
  }, [attempt, onSessionExpired]);

  async function run(action: 'create' | 'more' | 'save', task: () => Promise<void>) {
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

  function create() {
    void run('create', async () => {
      const created = await execute.current<Interview>('/interviews', {});
      onOpen(created.id);
    });
  }

  function saveProfile(context: Context) {
    void run('save', async () => {
      setProfile(await command<Context>('/interview-profile', context, undefined, 'PUT'));
      setEditing(false);
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
  if (!page || !catalog) return <p role="status">Загружаем интервью…</p>;

  const sorted = [...page.items].sort((a, b) => b.updated_at.localeCompare(a.updated_at));
  const visible = historyExpanded ? sorted : sorted.slice(0, previewCount);
  const showHistoryToggle = historyExpanded || sorted.length > previewCount || !!page.next_cursor;

  return <div className="workspace">
    <section className="panel interview-start" aria-labelledby="new-title">
      <h2 id="new-title">{!profile || editing ? 'О вашей работе' : 'Новое интервью'}</h2>
      {!profile || editing ? <>
        <p className="interview-profile-intro">Выберите одну профессию. Остальное можно пропустить.
          Ответы сохраняются для следующих интервью.</p>
        <ProfileForm profile={profile} catalog={catalog} busy={!!busy} onSave={saveProfile} onCancel={() => {
          setEditing(false);
          setActionError('');
        }} />
      </> : <>
        <p>Вспомните рабочий случай, о котором хотите рассказать.</p>
        <div className="interview-profile-summary">
          <div><span>Ваша анкета</span><strong>{profile.role}</strong>
            <span>Тон интервьюера: {tones[profile.tone ?? 'friendly'].toLowerCase()}</span></div>
          <button type="button" className="quiet" disabled={!!busy} onClick={() => setEditing(true)}>Изменить анкету</button>
        </div>
        <button disabled={!!busy} onClick={create}>{busy === 'create' ? 'Создаём…' : 'Начать интервью'}</button>
      </>}
    </section>
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
