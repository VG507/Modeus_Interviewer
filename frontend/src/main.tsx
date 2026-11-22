import { Fragment, useCallback, useEffect, useRef, useState, type FormEvent } from 'react';
import { createRoot } from 'react-dom/client';
import { api, ApiError, command, describe } from './api';
import { InterviewHome } from './InterviewHome';
import { InterviewScreen } from './InterviewScreen';
import { EditorData, dataTabForRoute } from './EditorData';
import { CardsList, CardScreen, CatalogCard } from './Cards';
import { Reports } from './Reports';
import { Themes } from './Themes';
import { ThemeToggle } from './ThemeToggle';
import './style.css';

type User = { id: string; is_editor: boolean; is_demo?: boolean };
type DemoRole = 'participant' | 'moderator';

function App() {
  const [user, setUser] = useState<User | null>(null);
  const [demoEnabled, setDemoEnabled] = useState(false);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [route, setRoute] = useState(() => window.location.hash);
  const interviewId = route.match(/^#\/interviews\/([a-zA-Z0-9_-]+)$/)?.[1];
  const cardId = route.match(/^#\/cards\/([a-zA-Z0-9_-]+)$/)?.[1];
  const themesView = route === '#/catalog/themes';
  const accountLogin = route === '#/account/login';
  const showAccountLogin = accountLogin && (!user || user.is_demo);
  const demoModerator = user?.is_demo && user.is_editor;
  const canManageData = user?.is_editor && !user.is_demo;
  const reviewView = route === '#/review' || route === '#/review/published' ||
    (demoModerator && (route === '' || route === '#'));
  const catalogId = themesView ? undefined : route.match(/^#\/catalog\/([a-zA-Z0-9_-]+)$/)?.[1];
  const dataView = dataTabForRoute(route);
  const lock = useRef(false);

  useEffect(() => {
    const controller = new AbortController();
    const options = { signal: controller.signal };
    const loadUser = api<User>('/me', options).then(setUser).catch(error => {
      if (!(error instanceof ApiError && error.status === 401)) throw error;
    });
    const loadDemo = api<{ enabled: boolean }>('/auth/demo', options).then(({ enabled }) => setDemoEnabled(enabled));
    Promise.allSettled([loadUser, loadDemo]).then(results => {
      const failed = results.find(result => result.status === 'rejected');
      if (!controller.signal.aborted && failed?.status === 'rejected') setError(describe(failed.reason));
    }).finally(() => {
      if (!controller.signal.aborted) setLoading(false);
    });
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
      if (accountLogin) {
        window.location.hash = '';
        setRoute('');
      }
    });
  }

  function enterDemo(role: DemoRole) {
    void auth(async () => {
      const nextUser = await command<User>('/auth/demo', { role, switch: !!user?.is_demo }).catch(e => {
        if (e instanceof ApiError && e.status === 401) expireSession();
        throw e;
      });
      const nextRoute = nextUser.is_editor ? '#/review' : '';
      window.location.hash = nextRoute;
      setRoute(nextRoute);
      setUser(nextUser);
    });
  }

  function logout() {
    void auth(async () => {
      await api<void>('/auth/logout', { method: 'POST' });
      window.location.hash = '';
      setRoute('');
      setUser(null);
    });
  }

  return <main>
    <header className="masthead">
      <span className="brand">Modeus</span>
      <nav className="actions" aria-label="Разделы Modeus">
        {!demoModerator && <a href="#">Интервью</a>}
        <a href="#/catalog">Каталог</a>
        {user && !demoModerator && <a href="#/cards">Мои карточки</a>}
        {user?.is_editor && <a href="#/review" aria-current={reviewView ? 'page' : undefined}>Модерация</a>}
        {canManageData && <a href="#/data" aria-current={dataView ? 'page' : undefined}>Данные</a>}
        {canManageData && <a href="#/report" aria-current={route === '#/report' ? 'page' : undefined}>Итоговый отчёт</a>}
        {!user && (route === '#/catalog' || catalogId || themesView) && <a href="#">Войти</a>}
      </nav>
      <div className="masthead-side">
        {user && <div className="account">{!user.is_demo &&
          <span>{user.is_editor ? 'Модератор' : 'Участник'}</span>}
          {user.is_demo && !accountLogin && <a href="#/account/login">Рабочий аккаунт</a>}
          <button className="quiet" disabled={busy} onClick={logout}>Выйти</button></div>}
        <ThemeToggle/>
        {user?.is_demo && <select className="role-switch" aria-label="Роль в демо" disabled={busy}
          value={user.is_editor ? 'moderator' : 'participant'}
          onChange={event => enterDemo(event.target.value as DemoRole)}>
          <option value="participant">Участник</option>
          <option value="moderator">Модератор</option>
        </select>}
      </div>
    </header>
    <Fragment key={user?.id ?? 'guest'}>
    {themesView ? <Themes/> : catalogId ? <CatalogCard key={catalogId} id={catalogId}/> : route === '#/catalog' ?
      <CardsList key="catalog" scope="catalog" onSessionExpired={expireSession}/> :
      loading ? <p role="status">Проверяем вход…</p> : !user || showAccountLogin ?
      demoEnabled && !showAccountLogin ?
      <section className="panel login demo-login" aria-labelledby="demo-heading">
        <h1 id="demo-heading">Демо Modeus</h1>
        <fieldset disabled={busy} aria-busy={busy}>
          <div>
            <button onClick={() => enterDemo('participant')}>Войти как участник</button>
            <p>Заполните анкету и пройдите интервью.</p>
          </div>
          <div>
            <button className="secondary" onClick={() => enterDemo('moderator')}>Войти как модератор</button>
            <p>Проверьте готовую карточку по вопросам, ответам и источникам.</p>
          </div>
        </fieldset>
        <p><a href="#/account/login">Войти в рабочий аккаунт</a></p>
      </section> :
      <form className="panel login" onSubmit={login}>
        <h1>Вход в Modeus</h1>
        <fieldset disabled={busy}>
          <label htmlFor="email">Почта</label>
          <input id="email" type="email" name="email" autoComplete="username" required maxLength={254}/>
          <label htmlFor="password">Пароль</label>
          <input id="password" type="password" name="password" autoComplete="current-password" required maxLength={256}/>
          <button>{busy ? 'Входим…' : 'Войти'}</button>
        </fieldset>
        {demoEnabled && showAccountLogin && <p><a href="#">Вернуться в демо</a></p>}
      </form> : cardId ? <CardScreen key={cardId} id={cardId} user={user} onSessionExpired={expireSession}/> :
      route === '#/cards' ? <CardsList key="own" scope="own" onSessionExpired={expireSession}/> :
      dataView ?
        canManageData ? <EditorData view={dataView} onSessionExpired={expireSession}/> :
          <p role="alert">Для этого раздела нужен доступ к управлению данными.</p> :
      route === '#/report' ?
        canManageData ? <Reports onSessionExpired={expireSession}/> :
          <p role="alert">Для этого раздела нужен доступ к управлению данными.</p> :
      reviewView ?
        user.is_editor ? <CardsList key={route} scope={route === '#/review/published' ? 'published' : 'review'}
          onSessionExpired={expireSession}/> : <p role="alert">Для этого раздела нужна роль модератора.</p> :
      interviewId ? <>
        <a className="back" href="#">← Мои интервью</a>
        <InterviewScreen key={interviewId} id={interviewId} onSessionExpired={expireSession}/>
      </> : <>
        <h1>Интервью о рабочем опыте</h1>
        <InterviewHome onOpen={id => { window.location.hash = `/interviews/${id}`; }} onSessionExpired={expireSession}/>
      </>}
    </Fragment>
    {error && <p role="alert">{error}</p>}
  </main>;
}

createRoot(document.getElementById('root')!).render(<App/>);
