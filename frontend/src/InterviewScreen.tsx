import { useCallback, useEffect, useRef, useState } from 'react';
import { api, ApiError, commandKeys, describe, processingError, type AnswerResult,
  type Card, type Detail, type Interview } from './api';

const stageLabels = {
  initial_question: 'Готовим первый вопрос',
  extraction: 'Разбираем ответ',
  question: 'Готовим уточнение',
  synthesis: 'Собираем карточку',
};

function CardPanel({ id, onSessionExpired }: { id: string; onSessionExpired: () => void }) {
  const [card, setCard] = useState<Card>();
  const [error, setError] = useState('');
  const [attempt, retry] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    setError('');
    api<Card>(`/cards/${encodeURIComponent(id)}`, { signal: controller.signal }).then(setCard).catch(error => {
      if (controller.signal.aborted) return;
      if (error instanceof ApiError && error.status === 401) onSessionExpired();
      else setError(describe(error));
    });
    return () => controller.abort();
  }, [id, attempt, onSessionExpired]);
  return <section className="panel" aria-labelledby="card-title">
      <h2 id="card-title">Карточка интервью</h2>
    {error ? <><p role="alert">{error}</p><button onClick={() => retry(n => n + 1)}>Повторить загрузку</button></>
        : card ? <>
          <div className="card-text">{card.text}</div>
          <a className="card-link" href={`#/cards/${card.id}`}>Открыть карточку</a>
        </> : <p role="status">Загружаем карточку…</p>}
  </section>;
}

export function InterviewScreen({ id, onSessionExpired }: { id: string; onSessionExpired: () => void }) {
  const path = `/interviews/${encodeURIComponent(id)}`;
  const [session, setSession] = useState<Detail>();
  const [busy, setBusy] = useState('');
  const [loadError, setLoadError] = useState('');
  const [actionError, setActionError] = useState('');
  const [editor, setEditor] = useState<{ question: string; text: string }>();
  const [notice, setNotice] = useState('');
  const [confirmFinish, setConfirmFinish] = useState(false);
  const execute = useRef(commandKeys());
  const locked = useRef(false);
  const alive = useRef(true);
  const read = useRef<AbortController | null>(null);
  const refresh = useCallback(async () => {
    read.current?.abort();
    const controller = new AbortController();
    read.current = controller;
    try {
      const current = await api<Detail>(path, { signal: controller.signal });
      if (controller.signal.aborted) return;
      setSession(previous => !previous || current.version >= previous.version ? current : previous);
      setLoadError('');
    } catch (error) {
      if (controller.signal.aborted) return;
      if (error instanceof ApiError && error.status === 401) onSessionExpired();
      else setLoadError(describe(error));
    }
  }, [path, onSessionExpired]);
  const processing = session?.processing_status === 'queued' || session?.processing_status === 'running';
  useEffect(() => {
    alive.current = true;
    void refresh();
    return () => { alive.current = false; read.current?.abort(); };
  }, [refresh]);
  useEffect(() => {
    if (!processing || busy) return;
    let stopped = false;
    let timer: number;
    const poll = async () => {
      await refresh();
      if (!stopped) timer = window.setTimeout(poll, 900);
    };
    timer = window.setTimeout(poll, 900);
    return () => { stopped = true; window.clearTimeout(timer); };
  }, [processing, busy, refresh]);

  const currentQuestion = session?.messages.find(message => message.id === session.current_question_id);
  const draft = session?.messages.find(message =>
    message.status === 'draft' && message.reply_to_question_id === session.current_question_id);
  const text = editor && editor.question === currentQuestion?.id ? editor.text : draft?.text ?? '';
  const unsaved = text.trim() !== (draft?.text ?? '');
  const canAnswer = !!currentQuestion && session?.status === 'active' && session.processing_status === 'idle';

  function showAccepted(result: Interview, message?: Detail['messages'][number]) {
    setSession(previous => {
      if (!previous || previous.version > result.version) return previous;
      const messages = !message ? previous.messages
        : previous.messages.some(item => item.id === message.id)
          ? previous.messages.map(item => item.id === message.id ? message : item)
          : [...previous.messages, message];
      return { ...previous, ...result, messages };
    });
  }

  async function run(label: string, operation: () => Promise<void>) {
    if (locked.current) return;
    locked.current = true;
    setBusy(label);
    setActionError('');
    setNotice('');
    read.current?.abort();
    try {
      await operation();
    } catch (error) {
      if (!alive.current) return;
      if (error instanceof ApiError && error.status === 401) onSessionExpired();
      else setActionError(describe(error));
    } finally {
      locked.current = false;
      if (alive.current) {
        setBusy('');
        void refresh();
      }
    }
  }

  async function saveAnswer(send: boolean) {
    if (!session || !currentQuestion || !text.trim()) return;
    await run(send ? 'Отправляем…' : 'Сохраняем…', async () => {
      let version = session.version;
      let answerId = draft?.id;
      if (!draft || unsaved) {
        const result = draft
          ? await execute.current<AnswerResult>(`${path}/answers/${draft.id}`,
              { expected_version: version, text: text.trim() }, 'PATCH')
          : await execute.current<AnswerResult>(`${path}/answers`,
              { expected_version: version, question_id: currentQuestion.id, text: text.trim() });
        showAccepted(result.interview, result.answer);
        version = result.interview.version;
        answerId = result.answer.id;
        setNotice('Черновик сохранён.');
      }
      if (send && answerId) {
        const result = await execute.current<AnswerResult>(`${path}/answers/${answerId}/submit`,
          { expected_version: version });
        showAccepted(result.interview, result.answer);
        setNotice('');
        setEditor(undefined);
      }
    });
  }

  function action(name: 'skip' | 'finish' | 'retry', label: string) {
    if (!session) return;
    void run(label, async () => {
      const result = await execute.current<Interview>(`${path}/${name}`, { expected_version: session.version });
      showAccepted(result);
      setConfirmFinish(false);
      if (name === 'skip') setEditor(undefined);
    });
  }

  if (!session) return <section className="panel">{loadError
    ? <><p role="alert">{loadError}</p><button onClick={refresh}>Повторить загрузку</button></>
    : <p role="status">Загружаем интервью…</p>}</section>;

  return <div className="stack">
    <h1>{session.current_topic_title || 'Интервью'}</h1>
    <section className="panel">
      <h2>Диалог</h2>
      <ol className="transcript" aria-label="История интервью">
        {session.messages.filter(message => message.status !== 'draft').map(message => {
          const source = message.source_quote ?? message.vacancy_quote;
          return <li key={message.id} className={`turn turn-${message.kind}`}>
            {message.kind !== 'control' && <strong>{message.kind === 'question' ? 'Modeus' : 'Вы'}</strong>}
            <p>{message.text}</p>
            {source && <details className="question-source">
              <summary>Источник уточнения</summary>
              <blockquote>{source.quote}</blockquote>
              <a href={source.url} target="_blank" rel="noreferrer">{source.title}</a>
            </details>}
            {message.assessment === 'unclear' && <small>Нужно уточнение</small>}
            {message.assessment === 'unrelated' && <small>Связь с рабочим случаем неясна</small>}
          </li>;
        })}
      </ol>
      {processing && <p className="status" role="status">
        {session.processing_stage ? stageLabels[session.processing_stage] : 'Обрабатываем интервью'}…
      </p>}
      {loadError && <div className="feedback"><p role="alert">{loadError}</p>
        <button className="secondary" disabled={!!busy} onClick={refresh}>Обновить состояние</button></div>}
      {actionError && <p role="alert">{actionError}</p>}
      {session.processing_status === 'failed' && <div className="feedback">
        <p role="alert">{processingError(session.error?.code ?? '')}</p>
        {session.error?.retryable && <button disabled={!!busy || !!loadError} onClick={() => action('retry', 'Повторяем…')}>Повторить обработку</button>}
      </div>}
      {canAnswer && currentQuestion && <form className="composer" onSubmit={event => { event.preventDefault(); void saveAnswer(true); }}>
        <label htmlFor="reply">Ваш ответ</label>
        <textarea id="reply" rows={5} required maxLength={8000} value={text} disabled={!!busy}
          onChange={event => setEditor({ question: currentQuestion.id, text: event.target.value })}/>
        <div className="actions">
          <button disabled={!!busy || !!loadError || !text.trim()}>Отправить ответ</button>
          <button type="button" className="secondary" disabled={!!busy || !!loadError || !text.trim() || !unsaved}
            onClick={() => void saveAnswer(false)}>Сохранить черновик</button>
          <button type="button" className="quiet" disabled={!!busy || !!loadError}
            onClick={() => action('skip', 'Пропускаем…')}>Пропустить вопрос</button>
        </div>
      </form>}
      {busy && <p role="status">{busy}</p>}
      {notice && <p role="status">{notice}</p>}
      {session.status === 'active' && <div className="finish">
        {confirmFinish ? <>
          <p>Собрать карточку из отправленных ответов? Черновик ответа в карточку не войдёт.</p>
          <div className="actions">
            <button disabled={!!busy || !!loadError} onClick={() => action('finish', 'Завершаем…')}>Завершить и собрать карточку</button>
            <button className="secondary" disabled={!!busy} onClick={() => setConfirmFinish(false)}>Продолжить интервью</button>
          </div>
        </> : <button className="secondary" disabled={!!busy || !!loadError}
          onClick={() => setConfirmFinish(true)}>Завершить интервью</button>}
      </div>}
      {session.status === 'completed' && <p className="status">Интервью завершено.</p>}
    </section>
    {session.card_id && <CardPanel key={session.card_id} id={session.card_id} onSessionExpired={onSessionExpired}/>}
  </div>;
}
