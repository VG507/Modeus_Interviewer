import { useEffect, useRef, useState, type FormEvent } from 'react';
import { api, ApiError, commandKeys, describe } from './api';
import type {
  Card, CardContent, CardDetail, CardEvidence, CardFact, CardGap, Page, PublicCard, PublicSummary, SearchPage,
} from './api';

type User = { id: string; is_editor: boolean };
type SessionProps = { onSessionExpired: () => void };

function status(card: Card) {
  if (card.publication_status === 'published') return 'Опубликована';
  if (card.publication_status === 'in_review') return 'На проверке';
  if (card.review_status === 'needs_changes') return 'Нужны правки';
  return 'Приватный черновик';
}

export function CardsList({ scope, onSessionExpired }: SessionProps & {
  scope: 'own' | 'review' | 'published' | 'catalog';
}) {
  const [items, setItems] = useState<{ id: string; title: string; label: string }[]>([]);
  const [cursor, setCursor] = useState<string | null>();
  const [next, setNext] = useState<string | null>();
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [attempt, setAttempt] = useState(0);
  const [topics, setTopics] = useState<string[]>([]);
  const [topic, setTopic] = useState('');
  const [topicError, setTopicError] = useState('');
  const [query, setQuery] = useState('');
  const [search, setSearch] = useState('');
  const [found, setFound] = useState<SearchPage>();
  const [searchError, setSearchError] = useState('');
  const isCatalog = scope === 'catalog';

  useEffect(() => {
    if (!search) return;
    const controller = new AbortController();
    setFound(undefined);
    setSearchError('');
    api<SearchPage>('/catalog/search?' + new URLSearchParams({ q: search }), { signal: controller.signal })
      .then(page => { if (!controller.signal.aborted) setFound(page); })
      .catch(error => { if (!controller.signal.aborted) setSearchError(describe(error)); });
    return () => controller.abort();
  }, [search]);

  useEffect(() => {
    if (!isCatalog) return;
    const controller = new AbortController();
    api<{ items: string[] }>('/catalog/topics', { signal: controller.signal })
      .then(page => { setTopics(page.items); setTopicError(''); })
      .catch(error => { if (!controller.signal.aborted) setTopicError(describe(error)); });
    return () => controller.abort();
  }, [isCatalog, attempt]);

  useEffect(() => {
    const controller = new AbortController();
    const params = new URLSearchParams();
    if (!isCatalog) params.set('scope', scope);
    if (isCatalog && topic) params.set('topic_title', topic);
    if (cursor) params.set('cursor', cursor);
    const path = (isCatalog ? '/catalog' : '/cards') + '?' + params;
    setLoading(true);
    setError('');
    api<Page<Card | PublicSummary>>(path, { signal: controller.signal }).then(page => {
      if (controller.signal.aborted) return;
      const entries = page.items.map(card => 'title' in card ? {
        id: card.id, title: card.title, label: [card.topic_title !== card.title ? card.topic_title : '', card.author_name]
          .filter(Boolean).join(' · '),
      } : { id: card.id, title: card.content.title, label: status(card) });
      setItems(previous => cursor ? [...previous, ...entries] : entries);
      setNext(page.next_cursor);
    }).catch(error => {
      if (controller.signal.aborted) return;
      if (!isCatalog && error instanceof ApiError && error.status === 401) onSessionExpired();
      else setError(describe(error));
    }).finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [scope, isCatalog, onSessionExpired, cursor, attempt, topic]);

  const headings = { catalog: 'Каталог карточек', own: 'Мои карточки', review: 'На проверке', published: 'Опубликованные' };
  const empty = { catalog: 'Опубликованных карточек пока нет.', own: 'У вас пока нет карточек.',
    review: 'Карточек на проверке пока нет.', published: 'Опубликованных карточек пока нет.' };

  return <>
    {scope === 'review' || scope === 'published' ? <nav className="actions cards-nav" aria-label="Редактура">
      <a href="#/review" aria-current={scope === 'review' ? 'page' : undefined}>На проверке</a>
      <a href="#/review/published" aria-current={scope === 'published' ? 'page' : undefined}>Опубликованные</a>
    </nav> : null}
    <h1>{headings[scope]}</h1>
    <section className="panel saved">
      {isCatalog && <form className="composer" role="search" onSubmit={event => {
        event.preventDefault();
        setSearch(query.trim());
      }}>
        <label htmlFor="catalog-search">Поиск по смыслу</label>
        <div className="actions">
          <input id="catalog-search" type="search" maxLength={200} value={query}
            placeholder="Например: отчёт строится слишком долго" onChange={event => {
              setQuery(event.target.value);
              if (!event.target.value.trim()) setSearch('');
            }}/>
          <button>Найти</button>
        </div>
      </form>}
      {search ? <>
        {found?.mode === 'words' && <p className="meta">Поиск по смыслу сейчас недоступен, ищем по совпадению слов.</p>}
        {!!found?.items.length && <ul>{found.items.map(card => <li key={card.id}>
          <a href={`#/catalog/${card.id}`}>
            {card.title}{card.topic_title !== card.title && <span>{card.topic_title}</span>}
          </a>
        </li>)}</ul>}
        {found && !found.items.length && <p>Похожих карточек не нашлось.</p>}
        {!found && !searchError && <p role="status">Ищем похожие карточки…</p>}
        {searchError && <p role="alert">{searchError}</p>}
      </> : <>
      {isCatalog && topics.length > 0 && <div className="composer">
        <label htmlFor="catalog-topic">Тема</label>
        <select id="catalog-topic" value={topic} onChange={event => {
          setTopic(event.target.value);
          setCursor(null);
          setNext(null);
          setItems([]);
          setLoading(true);
        }}>
          <option value="">Все темы</option>
          {topics.map(item => <option key={item}>{item}</option>)}
        </select>
      </div>}
      {isCatalog && topicError && <div>
        <p role="alert">Темы каталога недоступны. {topicError}</p>
        <button className="secondary" onClick={() => { setCursor(null); setAttempt(value => value + 1); }}>
          Повторить загрузку тем
        </button>
      </div>}
      {items.length > 0 && <ul>{items.map(card => <li key={card.id}>
        <a href={`#/${isCatalog ? 'catalog' : 'cards'}/${card.id}`}>
          {card.title}{card.label && <span>{card.label}</span>}
        </a>
      </li>)}</ul>}
      {loading && <p role="status">Загружаем карточки…</p>}
      {!loading && !error && items.length === 0 && <p>{isCatalog && topic ? 'По этой теме карточек пока нет.' : empty[scope]}</p>}
      {error && <><p role="alert">{error}</p><button onClick={() => { setCursor(null); setAttempt(value => value + 1); }}>
        Повторить загрузку
      </button></>}
      {next && !error && <button className="secondary" disabled={loading} onClick={() => setCursor(next)}>Показать ещё</button>}
      </>}
    </section>
  </>;
}

export function CatalogCard({ id }: { id: string }) {
  const [card, setCard] = useState<PublicCard>();
  const [error, setError] = useState('');
  const [similar, setSimilar] = useState<SearchPage['items']>([]);
  useEffect(() => {
    const controller = new AbortController();
    api<PublicCard>(`/catalog/${id}`, { signal: controller.signal }).then(setCard).catch(error => {
      if (!controller.signal.aborted) setError(describe(error));
    });
    // Similar cases are a hint: the card reads fine without them, so a failure stays quiet.
    api<SearchPage>(`/catalog/${id}/similar`, { signal: controller.signal })
      .then(page => { if (!controller.signal.aborted) setSimilar(page.items); }).catch(() => undefined);
    return () => controller.abort();
  }, [id]);

  return <>
    <a className="back" href="#/catalog">← Каталог</a>
    {error ? <p role="alert">{error}</p> : card ? <article className="panel">
      <h1>{card.title}</h1>
      {card.author_name && <p>Автор: {card.author_name}</p>}
      <div className="card-text">{card.text}</div>
      <CardGaps content={card.content}/>
    </article> : <p role="status">Загружаем карточку…</p>}
    {card && similar.length > 0 && <section className="panel saved" aria-label="Похожие случаи">
      <h2>Похожие случаи</h2>
      <ul>{similar.map(item => <li key={item.id}><a href={`#/catalog/${item.id}`}>
        {item.title}{item.topic_title !== item.title && <span>{item.topic_title}</span>}
      </a></li>)}</ul>
    </section>}
  </>;
}

const sections = {
  task: 'Задача', context: 'Контекст', actions: 'Действия', decisions: 'Решения',
  verification: 'Проверка', errors: 'Ошибки', limitations: 'Ограничения',
} as const;

const gapLabels: Record<CardGap, string> = {
  task: sections.task, context: sections.context, action: sections.actions, decision: sections.decisions,
  verification: sections.verification, error: sections.errors, limitation: sections.limitations,
};

function hasStructuredContent(content: CardContent) {
  return !!(content.cases?.length || content.unknown_fields?.length || Object.keys(sections)
    .some(key => content[key as keyof typeof sections]?.length));
}

function hasGaps(content: CardContent): boolean {
  return !!content.unknown_fields?.length || !!content.cases?.some(hasGaps);
}

function CardGaps({ content }: { content: CardContent }) {
  return <>
    {!!content.unknown_fields?.length && <div>
      <strong>Что осталось невыясненным</strong>
      <ul>{[...new Set(content.unknown_fields)].map(gap => <li key={gap}>{gapLabels[gap] ?? gap}</li>)}</ul>
    </div>}
    {content.cases?.filter(hasGaps).map((item, index) => <section key={index}>
      <h3>{item.title}</h3><CardGaps content={item}/>
    </section>)}
  </>;
}

function Evidence({ path, version, onSessionExpired }: { path: string; version: number; onSessionExpired: () => void }) {
  const [open, setOpen] = useState(false);
  const [result, setResult] = useState<CardEvidence>();
  const [error, setError] = useState('');
  useEffect(() => {
    if (!open) return;
    const controller = new AbortController();
    setResult(undefined);
    setError('');
    api<CardEvidence>(path, { signal: controller.signal }).then(value => {
      if (!controller.signal.aborted) setResult(value);
    }).catch(problem => {
      if (controller.signal.aborted) return;
      if (problem instanceof ApiError && problem.status === 401) onSessionExpired();
      else setError(describe(problem));
    });
    return () => controller.abort();
  }, [path, version, open, onSessionExpired]);
  return <details onToggle={event => setOpen(event.currentTarget.open)}>
    <summary>Доказательства</summary>
    {error ? <p role="alert">{error}</p> : !result ? <p role="status">Загружаем доказательства…</p> : <>
      {result.original_text && <p><strong>Исходная формулировка</strong> {result.original_text}</p>}
      {!!result.skills.length && <p>Подтверждённые навыки · {result.skills.join(', ')}</p>}
      {!result.turns.length && <p>Для этой формулировки нет подтверждённой связи с ответом.</p>}
      {result.turns.map((turn, index) => <div key={index}>
        <p><strong>Вопрос</strong> {turn.question}</p>
        <p><strong>Цитата из ответа</strong> {turn.answer_quote}</p>
        {turn.source && <><p><a href={turn.source.url} target="_blank" rel="noreferrer">{turn.source.title}</a></p>
          <blockquote>{turn.source.quote}</blockquote></>}
      </div>)}
    </>}
  </details>;
}

type FactTools = { path: string; version: number; onSessionExpired: () => void };

function Facts({ content, evidence, onEdit }: {
  content: CardContent; evidence?: FactTools; onEdit?: (fact: CardFact) => void;
}) {
  return <>
    {Object.entries(sections).map(([key, label]) => {
      const facts = content[key as keyof typeof sections];
      return facts?.length ? <div key={key}><strong>{label}</strong>
        <ul>{facts.map((fact, index) => <li key={fact.id ?? index}>
          {fact.text}
          {!!fact.skills?.length && <p className="meta">{fact.skills.join(', ')}</p>}
          {fact.derived_from_fact_id && <p className="meta">Правка автора, связанная с исходным фактом</p>}
          {fact.id && evidence && <Evidence {...evidence} path={`${evidence.path}/facts/${encodeURIComponent(fact.id)}/evidence`}/>}
          {fact.id && onEdit && <button type="button" className="quiet" onClick={() => onEdit(fact)}>Исправить факт</button>}
        </li>)}</ul>
      </div> : null;
    })}
    <CardGaps content={{ title: content.title, unknown_fields: content.unknown_fields }}/>
    {content.cases?.map((item, index) => <section key={index}>
      <h3>{item.title}</h3><Facts content={item} evidence={evidence} onEdit={onEdit}/>
    </section>)}
  </>;
}

function editFact(content: CardContent, target: string, text: string): CardContent {
  const edited: CardContent = { ...content };
  for (const section of Object.keys(sections) as (keyof typeof sections)[]) {
    edited[section] = content[section]?.map(fact => fact.id === target ? { id: target, text } : fact);
  }
  if (content.cases) edited.cases = content.cases.map(item => editFact(item, target, text));
  return edited;
}

function structuredText(content: CardContent): string {
  const paragraphs = [content.title];
  for (const section of Object.keys(sections) as (keyof typeof sections)[]) {
    const facts = content[section];
    if (facts?.length) paragraphs.push(sections[section] + '\n' + facts.map(fact => `• ${fact.text}`).join('\n'));
  }
  if (content.unknown_fields?.length) paragraphs.push('Что осталось невыясненным: ' +
    content.unknown_fields.map(field => gapLabels[field]).join(', '));
  return [...paragraphs, ...(content.cases ?? []).map(structuredText)].join('\n\n');
}

export function CardScreen({ id, user, onSessionExpired }: SessionProps & { id: string; user: User }) {
  const path = `/cards/${id}`;
  const [card, setCard] = useState<Card>();
  const [reference, setReference] = useState<CardContent>();
  const [title, setTitle] = useState('');
  const [text, setText] = useState('');
  const [showAuthor, setShowAuthor] = useState(false);
  const [author, setAuthor] = useState('');
  const [consent, setConsent] = useState(false);
  const [shareEvidence, setShareEvidence] = useState(false);
  const [factEdit, setFactEdit] = useState<CardFact & { originalText: string }>();
  const [comment, setComment] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [conflict, setConflict] = useState(false);
  const [reload, setReload] = useState(0);
  const [execute] = useState(commandKeys);
  const lock = useRef(false);

  function show(data: Card) {
    setCard(data);
    setTitle(data.content.title);
    setText(data.text);
    setShowAuthor(data.show_author);
    setAuthor(data.author_name ?? '');
    setConsent(false);
    setShareEvidence(false);
    setFactEdit(undefined);
    setComment('');
    setConflict(false);
  }

  useEffect(() => {
    const controller = new AbortController();
    setError('');
    api<CardDetail>(path, { signal: controller.signal }).then(data => {
      if (controller.signal.aborted) return;
      const previous = data.history.slice().reverse().map(event => event.details.previous?.content)
        .find(content => content && hasStructuredContent(content));
      setReference(previous ?? data.original.content);
      show(data);
    }).catch(error => {
      if (controller.signal.aborted) return;
      if (error instanceof ApiError && error.status === 401) onSessionExpired();
      else setError(describe(error));
    });
    return () => controller.abort();
  }, [path, reload, onSessionExpired]);

  async function action(suffix: string, payload: object, success: string, method = 'POST') {
    if (lock.current || !card || conflict) return;
    lock.current = true;
    setBusy(true);
    setError('');
    setNotice('');
    try {
      const updated = await execute<Card>(path + suffix, { expected_version: card.version, ...payload }, method);
      if (method === 'PATCH' && hasStructuredContent(card.content)) setReference(card.content);
      show(updated);
      setNotice(success);
    } catch (error) {
      if (error instanceof ApiError && error.status === 401) onSessionExpired();
      else {
        setError(describe(error));
        if (error instanceof ApiError && error.code === 'VERSION_CONFLICT') setConflict(true);
      }
    } finally {
      lock.current = false;
      setBusy(false);
    }
  }

  if (!card) return error ? <p role="alert">{error}</p> : <p role="status">Загружаем карточку…</p>;
  const owner = card.owner_id === user.id;
  const published = card.publication_status === 'published';
  const inReview = card.publication_status === 'in_review';
  const dirty = title.trim() !== card.content.title || text !== card.text;
  const hasFacts = hasStructuredContent(card.content);
  const disabled = busy || conflict;
  const evidence = owner || (user.is_editor && (inReview || published) && card.consent?.share_evidence)
    ? { path, version: card.version, onSessionExpired } : undefined;

  function save(event: FormEvent) {
    event.preventDefault();
    void action('', { title, text }, 'Правки сохранены.', 'PATCH');
  }

  function submit(event: FormEvent) {
    event.preventDefault();
    if (card?.review_blocker || dirty) return;
    void action('/submit-review', { consent, show_author: showAuthor, author_name: showAuthor ? author : null,
      share_evidence: shareEvidence },
      'Карточка отправлена редактору.');
  }

  function saveFact(event: FormEvent) {
    event.preventDefault();
    if (!card || !factEdit?.id || !factEdit.text.trim() || factEdit.text.trim() === factEdit.originalText || dirty) return;
    const content = editFact(card.content, factEdit.id, factEdit.text.trim());
    void action('', { content, text: structuredText(content) }, 'Правка факта сохранена.', 'PATCH');
  }

  function review(decision: 'accept' | 'return' | 'keep_private', message: string) {
    void action('/review', { decision, comment }, message);
  }

  return <>
    <nav className="actions cards-nav" aria-label="Карточка">
      {owner && <><a href="#/cards">← Мои карточки</a><a href={`#/interviews/${card.session_id}`}>Открыть интервью</a></>}
      {user.is_editor && <a href={published ? '#/review/published' : '#/review'}>К редактуре</a>}
      {published && <a href={`#/catalog/${card.id}`}>Открыть в каталоге</a>}
    </nav>
    <h1>{card.content.title}</h1>
    <div className="stack">
      <section className="panel">
        <p className="status">{status(card)}</p>
        {card.review?.comment && <p className="card-text">Комментарий редактора: {card.review.comment}</p>}
        {owner && !published ? <form className="composer" onSubmit={save}>
          <fieldset disabled={disabled}>
            <label htmlFor="card-title-edit">Название</label>
            <input id="card-title-edit" value={title} onChange={event => setTitle(event.target.value)} required maxLength={200}/>
            <label htmlFor="card-text-edit">Текст карточки</label>
            <textarea id="card-text-edit" rows={14} value={text} onChange={event => setText(event.target.value)}
              required maxLength={100000}/>
            {inReview && <p>После правки нужно заново отправить карточку на проверку.</p>}
            <button disabled={!dirty || !title.trim() || !text.trim()}>Сохранить правки</button>
          </fieldset>
        </form> : <div className="card-text">{card.text}</div>}
        {(owner || (user.is_editor && (inReview || published))) && <div className="composer">
          <p>{card.show_author ? `Отображаемое имя: ${card.author_name}` : 'Публикация без имени автора.'}</p>
          {hasFacts && <details><summary>Факты и доказательства</summary>
            <Facts content={card.content} evidence={evidence} onEdit={owner && !published && !dirty && !disabled
              ? fact => setFactEdit({ ...fact, originalText: fact.text }) : undefined}/>
          </details>}
          {factEdit && <form className="composer" onSubmit={saveFact}>
            <label htmlFor="fact-edit">Формулировка факта</label>
            <textarea id="fact-edit" value={factEdit.text} required maxLength={8000}
              onChange={event => setFactEdit({ ...factEdit, text: event.target.value })}/>
            <div className="actions"><button disabled={disabled || dirty || !factEdit.text.trim() ||
              factEdit.text.trim() === factEdit.originalText}>Сохранить факт</button>
              <button type="button" className="secondary" onClick={() => setFactEdit(undefined)}>Отмена</button></div>
          </form>}
          {!hasFacts && reference && hasStructuredContent(reference) && <details>
            <summary>Факты из предыдущей версии</summary>
            <p>Видны только в редактуре. В каталоге публикуется согласованная версия карточки.</p>
            <Facts content={reference} evidence={owner ? evidence : undefined}/>
          </details>}
        </div>}
      </section>

      {owner && card.publication_status === 'draft' && <form className="panel" onSubmit={submit}>
        <h2>Отправить на проверку</h2>
        <fieldset disabled={disabled || dirty}>
          <label className="check"><input type="checkbox" checked={consent}
            onChange={event => setConsent(event.target.checked)} required/> Согласен на публикацию этой версии после проверки редактором</label>
          <label className="check"><input type="checkbox" checked={shareEvidence}
            onChange={event => setShareEvidence(event.target.checked)}/> Показать редактору вопросы, источники и исходные цитаты к фактам этой версии</label>
          <label className="check"><input type="checkbox" checked={showAuthor}
            onChange={event => setShowAuthor(event.target.checked)}/> Указать имя автора в каталоге</label>
          {showAuthor && <>
            <label htmlFor="card-author">Отображаемое имя или псевдоним</label>
            <input id="card-author" value={author} onChange={event => setAuthor(event.target.value)} required maxLength={120}/>
          </>}
          <button disabled={!!card.review_blocker || !consent || (showAuthor && !author.trim())}>Отправить редактору</button>
        </fieldset>
        {dirty && <p className="feedback">Сначала сохраните правки.</p>}
        {!dirty && card.review_blocker && <p className="feedback">{card.review_blocker}</p>}
      </form>}

      {user.is_editor && (inReview || published) && <section className="panel">
        <h2>{published ? 'Управление публикацией' : 'Решение редактора'}</h2>
        {inReview && <p>Проверьте текст, структурированные факты и отображаемое имя на персональные и закрытые сведения.</p>}
        <fieldset disabled={disabled || dirty}>
          <label htmlFor="card-comment">Комментарий <span>{published ? 'обязателен' : 'обязателен при возврате'}</span></label>
          <textarea id="card-comment" rows={3} value={comment}
            onChange={event => setComment(event.target.value)} maxLength={4000}/>
          <div className="actions">
            {published ? <button className="secondary" disabled={!comment.trim()} onClick={() =>
              void action('/unpublish', { comment }, 'Карточка снята с публикации.')}>Снять с публикации</button> : <>
              <button onClick={() => review('accept', 'Карточка опубликована.')}>Опубликовать</button>
              <button className="secondary" disabled={!comment.trim()} onClick={() =>
                review('return', 'Карточка возвращена автору.')}>Вернуть на доработку</button>
              <button className="quiet" onClick={() => review('keep_private', 'Карточка оставлена приватной.')}>Оставить приватной</button>
            </>}
          </div>
        </fieldset>
      </section>}
      {notice && <p role="status">{notice}</p>}
      {error && <p role="alert">{error}</p>}
      {conflict && <div><button className="secondary" onClick={() => setReload(value => value + 1)}>Загрузить актуальную версию</button></div>}
    </div>
  </>;
}
