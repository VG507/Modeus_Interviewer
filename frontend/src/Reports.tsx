import { useEffect, useRef, useState, type FormEvent } from 'react';
import { api, ApiError, describe, download } from './api';

type Profession = { id: string; title: string; vacancies: number; demo: boolean };

const DEFAULT_DAYS = 90;

function isoDay(value: Date) {
  return new Date(value.getTime() - value.getTimezoneOffset() * 60_000).toISOString().slice(0, 10);
}

export function Reports({ onSessionExpired }: { onSessionExpired: () => void }) {
  const [open, setOpen] = useState(false);
  return <>
    <h1>Итоговый отчёт</h1>
    <section className="panel report-start">
      <p>Сводка по профессии в Excel: компетенции, навыки, знания, доказательная база и актуальность данных.
        Для каждого навыка и компетенции — насколько важно его изучать.</p>
      <button className="report-button" onClick={() => setOpen(true)}>Сделать итоговый отчёт</button>
    </section>
    {open && <ReportDialog onClose={() => setOpen(false)} onSessionExpired={onSessionExpired}/>}
  </>;
}

function ReportDialog({ onClose, onSessionExpired }: { onClose: () => void; onSessionExpired: () => void }) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [professions, setProfessions] = useState<Profession[]>();
  const [professionId, setProfessionId] = useState('');
  const [dateFrom, setDateFrom] = useState(() => isoDay(new Date(Date.now() - DEFAULT_DAYS * 86_400_000)));
  const [dateTo, setDateTo] = useState(() => isoDay(new Date()));
  const [demo, setDemo] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [saved, setSaved] = useState('');
  const selected = professions?.find(item => item.id === professionId);

  useEffect(() => { dialog.current?.showModal(); }, []);
  useEffect(() => {
    const controller = new AbortController();
    api<{ professions: Profession[] }>('/reports/professions', { signal: controller.signal }).then(result => {
      setProfessions(result.professions);
      const first = result.professions.find(item => item.vacancies || item.demo) ?? result.professions[0];
      if (first) choose(first);
    }).catch(problem => {
      if (controller.signal.aborted) return;
      if (problem instanceof ApiError && problem.status === 401) onSessionExpired();
      else setError(describe(problem));
    });
    return () => controller.abort();
  }, [onSessionExpired]);

  function choose(profession: Profession) {
    setProfessionId(profession.id);
    // Without collected vacancies the invented set is the only thing to show.
    setDemo(profession.demo && !profession.vacancies);
    setSaved('');
  }

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (busy || !selected) return;
    setBusy(true);
    setError('');
    setSaved('');
    try {
      setSaved(await download('/reports/profession',
        { profession_id: selected.id, date_from: dateFrom, date_to: dateTo, demo }, 'report.xlsx'));
    } catch (problem) {
      if (problem instanceof ApiError && problem.status === 401) onSessionExpired();
      else setError(describe(problem));
    } finally {
      setBusy(false);
    }
  }

  return <dialog ref={dialog} className="report-dialog" aria-labelledby="report-title" onClose={onClose}
    onCancel={event => { if (busy) event.preventDefault(); }}>
    <form onSubmit={submit}>
      <h2 id="report-title">Итоговый отчёт</h2>
      <fieldset disabled={busy}>
        <label htmlFor="report-profession">Профессия</label>
        <select id="report-profession" value={professionId} required onChange={event => {
          const profession = professions?.find(item => item.id === event.target.value);
          if (profession) choose(profession);
        }}>
          {!professions && <option value="">Загружаем…</option>}
          {professions?.map(item => <option key={item.id} value={item.id}>
            {item.title}{item.vacancies ? ` · вакансий: ${item.vacancies}` : item.demo ? ' · есть демо-данные' : ' · нет вакансий'}
          </option>)}
        </select>
        <div className="report-dates">
          <div>
            <label htmlFor="report-from">С</label>
            <input id="report-from" type="date" value={dateFrom} max={dateTo} required
              onChange={event => { setDateFrom(event.target.value); setSaved(''); }}/>
          </div>
          <div>
            <label htmlFor="report-to">По</label>
            <input id="report-to" type="date" value={dateTo} min={dateFrom} required
              onChange={event => { setDateTo(event.target.value); setSaved(''); }}/>
          </div>
        </div>
        {selected?.demo && <label className="check">
          <input type="checkbox" checked={demo} onChange={event => { setDemo(event.target.checked); setSaved(''); }}/>
          <span>Демо-данные: выдуманные вакансии и интервью для показа</span>
        </label>}
      </fieldset>
      {busy && <div className="market-progress" role="status">
        <span className="market-spinner" aria-hidden="true"/>
        <div><strong>Собираю отчёт…</strong>
          <div>Модель раскладывает данные по компетенциям, это занимает до двух минут.</div></div>
      </div>}
      {saved && <p className="report-saved" role="status">Готово: файл {saved} скачан.</p>}
      {error && <p role="alert">{error}</p>}
      <div className="report-actions">
        <button type="button" className="secondary" disabled={busy} onClick={() => dialog.current?.close()}>Закрыть</button>
        <button disabled={busy || !selected || !dateFrom || !dateTo || dateFrom > dateTo}>
          {busy ? 'Собираю…' : 'Сформировать'}
        </button>
      </div>
    </form>
  </dialog>;
}
