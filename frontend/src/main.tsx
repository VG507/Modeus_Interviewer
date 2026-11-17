import {useState, useEffect, type FormEvent} from 'react';
import {createRoot} from 'react-dom/client';
import './style.css';

type User = {id: string; is_editor: boolean};
function App() {
  const [user, setUser] = useState<User | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  useEffect(() => {fetch('/api/v1/me').then(async r => {
    if (r.ok) setUser(await r.json());
    else if (r.status !== 401) setError('Не удалось проверить вход.');
  }).catch(() => setError('Сервер недоступен.')).finally(() => setLoading(false));}, []);
  async function login(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); setBusy(true); setError('');
    const form = event.currentTarget;
    const data = new FormData(form);
    try {
      const r = await fetch('/api/v1/auth/login', {method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({email: data.get('email'), password: data.get('password')})});
      if (!r.ok) throw new Error(r.status === 429 ? 'Слишком много попыток. Повторите через 15 минут.' : 'Не удалось войти. Проверьте данные.');
      setUser(await r.json()); form.reset();
    } catch (e) {setError(e instanceof Error ? e.message : 'Сервер недоступен.');}
    finally {setBusy(false);}
  }
  async function logout() {
    setBusy(true); setError('');
    try {const r = await fetch('/api/v1/auth/logout', {method: 'POST'});
      if (!r.ok) throw new Error(); setUser(null);
    } catch {setError('Не удалось выйти. Повторите.');} finally {setBusy(false);}
  }
  return <main><p className="brand">MODEUS</p><h1>Профессиональный опыт начинается с разговора.</h1>
    {loading ? <p>Проверяем вход…</p> : user ? <section><h2>Вы вошли</h2>
      <p>{user.is_editor ? 'Доступ редактора' : 'Доступ участника'}</p>
      <p>Основа приложения готова. Интервью появится на следующем этапе разработки.</p>
      <button disabled={busy} onClick={logout}>Выйти</button></section> : <form onSubmit={login}>
      <h2>Вход участника</h2><label>Почта<input type="email" name="email" autoComplete="username" required maxLength={254}/></label>
      <label>Пароль<input type="password" name="password" autoComplete="current-password" required maxLength={256}/></label>
      <button disabled={busy}>{busy ? 'Входим…' : 'Войти'}</button></form>}
    {error && <p role="alert">{error}</p>}</main>;
}
createRoot(document.getElementById('root')!).render(<App/>);
