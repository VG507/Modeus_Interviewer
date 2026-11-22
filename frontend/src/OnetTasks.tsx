import { useEffect, useState } from 'react';
import { api, ApiError, describe } from './api';

type OnetProfile = {
  version: string;
  occupation_code: string;
  occupation_title: string;
  url: string;
  license_url: string;
  tasks: { id: number; text: string; type: string | null; importance: number | null; example_ru: string | null }[];
};

export function OnetTasks({ profession, onSessionExpired }: { profession: string; onSessionExpired: () => void }) {
  const [open, setOpen] = useState(false);
  const [profile, setProfile] = useState<OnetProfile | null | undefined>();
  const [error, setError] = useState('');

  useEffect(() => {
    if (!open) return;
    const controller = new AbortController();
    const { signal } = controller;
    setError('');
    api<OnetProfile | null>('/discovery/collection/' + encodeURIComponent(profession) + '/onet', { signal })
      .then(result => { if (!signal.aborted) setProfile(result); })
      .catch(problem => {
        if (signal.aborted) return;
        if (problem instanceof ApiError && problem.status === 401) onSessionExpired();
        else setError(describe(problem));
      });
    return () => controller.abort();
  }, [open, profession, onSessionExpired]);

  return <details className="panel" onToggle={event => setOpen(event.currentTarget.open)}>
    <summary>Рабочие задачи O*NET</summary>
    {error && <p role="alert">{error}</p>}
    {open && profile === undefined && !error && <p role="status">Загрузка задач…</p>}
    {profile === null && <p>Для этой профессии задачи O*NET пока не загружены.</p>}
    {profile && <>
      <p><a href={profile.url} target="_blank" rel="noreferrer" lang="en">{profile.occupation_title}</a>
        {' · '}{profile.occupation_code}{' · '}{profile.tasks.length} задач · рынок США</p>
      {profile.tasks.some(task => task.example_ru) && <>
        <h3>Примеры вопросов для интервью</h3>
        <ul>{profile.tasks.filter(task => task.example_ru).map(task => <li key={task.id}>{task.example_ru}</li>)}</ul>
      </>}
      <details>
        <summary>Все задачи в оригинале</summary>
        <ol>{profile.tasks.map(task => <li key={task.id}>
          <span lang="en">{task.text}</span>
          {task.type === 'Core' && ' · основная'}
          {task.type === 'Supplemental' && ' · дополнительная'}
          {task.importance !== null && <> · важность {task.importance.toLocaleString('ru-RU')}/5</>}
        </li>)}</ol>
      </details>
      <p className="meta">Данные O*NET® {profile.version}, U.S. Department of Labor, Employment and Training Administration.
        {' '}<a href={profile.license_url} target="_blank" rel="noreferrer">Лицензия CC BY 4.0</a>.
        Русские вопросы адаптированы Modeus.</p>
    </>}
  </details>;
}
