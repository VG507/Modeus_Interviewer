import { useCallback, useEffect, useRef, useState, type FormEvent } from 'react';
import { createRoot } from 'react-dom/client';
import { api, ApiError, command, describe } from './api';
import { InterviewHome } from './InterviewHome';
import { InterviewScreen } from './InterviewScreen';
import { EditorData } from './EditorData';
import { CardsList, CardScreen, CatalogCard } from './Cards';
import { Themes } from './Themes';
import { ThemeToggle } from './ThemeToggle';
import './style.css';

type User = { id: string; is_editor: boolean };

function App() {
  const [user, setUser] = useState<User | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [route, setRoute] = useState(() => window.location.hash);
  const interviewId = route.match(/^#\/interviews\/([a-zA-Z0-9_-]+)$/)?.[1];
  const cardId = route.match(/^#\/cards\/([a-zA-Z0-9_-]+)$/)?.[1];
  const themesView = route === '#/catalog/themes';
  const catalogId = themesView ? undefined : route.match(/^#\/catalog\/([a-zA-Z0-9_-]+)$/)?.[1];
  const dataView = route === '#/data' || route === '#/data/vacancies' || route === '#/data/sources' ||
    route === '#/vacancies' || route === '#/discovery';
  const lock = useRef(false);

  useEffect(() => {
    const controller = new AbortController();
    api<User>('/me', { signal: controller.signal }).then(setUser).catch(error => {
      if (!controller.signal.aborted && !(error instanceof ApiError && error.status === 401)) {
        setError(describe(error));
      }
    }).finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, []);

  useEffect(() => {
    function readLocation() { setRoute(window.location.hash); }
    window.addEventListener('hashchange', readLocation);
    return () => window.removeEventListener('hashchange', readLocation);
  }, []);

  const expireSession = useCallback(() => {
    setUser(null);
    setError('Сессия закончилась. Войдите снова.');
  }, []);

  async function auth(task: () => Promise<void>) {
    if (lock.current) return;
    lock.current = true;
    setBusy(true);
    setError('');
    try { await task(); }
    catch (e) { setError(describe(e)); }
    finally { lock.current = false; setBusy(false); }
  }

  function login(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const data = new FormData(event.currentTarget);
    void auth(async () => {
      setUser(await command<User>('/auth/login', { email: data.get('email'), password: data.get('password') }));
    });
  }

  function logout() {
    void auth(async () => {
      await api<void>('/auth/logout', { method: 'POST' });
      window.location.hash = '';
      setUser(null);
    });
  }

  return <main>
    <header className="masthead">
      <span className="brand">Modeus</span>
      <nav className="actions" aria-label="Разделы Modeus">
        <a href="#">Интервью</a>
        <a href="#/catalog">Каталог</a>
        {user && <a href="#/cards">Мои карточки</a>}
        {user?.is_editor && <a href="#/review">Редактура</a>}
        {user?.is_editor && <a href="#/data" aria-current={dataView ? 'page' : undefined}>Данные</a>}
        {!user && (route === '#/catalog' || catalogId || themesView) && <a href="#">Войти</a>}
      </nav>
      <div className="masthead-side">
        {user && <div className="account"><span>{user.is_editor ? 'Доступ редактора' : 'Доступ участника'}</span>
          <button className="quiet" disabled={busy} onClick={logout}>Выйти</button></div>}
        <ThemeToggle/>
      </div>
    </header>
    {themesView ? <Themes/> : catalogId ? <CatalogCard key={catalogId} id={catalogId}/> : route === '#/catalog' ?
      <CardsList key="catalog" scope="catalog" onSessionExpired={expireSession}/> :
      loading ? <p role="status">Проверяем вход…</p> : !user ?
      <form className="panel login" onSubmit={login}>
        <h1>Вход в Modeus</h1>
        <fieldset disabled={busy}>
          <label htmlFor="email">Почта</label>
          <input id="email" type="email" name="email" autoComplete="username" required maxLength={254}/>
          <label htmlFor="password">Пароль</label>
          <input id="password" type="password" name="password" autoComplete="current-password" required maxLength={256}/>
          <button>{busy ? 'Входим…' : 'Войти'}</button>
        </fieldset>
      </form> : cardId ? <CardScreen key={cardId} id={cardId} user={user} onSessionExpired={expireSession}/> :
      route === '#/cards' ? <CardsList key="own" scope="own" onSessionExpired={expireSession}/> :
      dataView ?
        user.is_editor ? <EditorData onSessionExpired={expireSession}/> :
          <p role="alert">Для этого раздела нужна роль редактора.</p> :
      route === '#/review' || route === '#/review/published' ?
        user.is_editor ? <CardsList key={route} scope={route === '#/review' ? 'review' : 'published'}
          onSessionExpired={expireSession}/> : <p role="alert">Для этого раздела нужен доступ редактора.</p> :
      interviewId ? <>
        <a className="back" href="#">← Мои интервью</a>
        <InterviewScreen key={interviewId} id={interviewId} onSessionExpired={expireSession}/>
      </> : <>
        <h1>Интервью о рабочем опыте</h1>
        <InterviewHome onOpen={id => { window.location.hash = `/interviews/${id}`; }} onSessionExpired={expireSession}/>
      </>}
    {error && <p role="alert">{error}</p>}
  </main>;
}

createRoot(document.getElementById('root')!).render(<App/>);
