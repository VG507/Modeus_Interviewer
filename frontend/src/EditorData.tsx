import { useCallback, useEffect, useState } from 'react';
import { api, ApiError, describe } from './api';
import { Discovery } from './Discovery';
import { ProfessionCollection } from './ProfessionCollection';
import { Vacancies } from './Vacancies';

type Profile = { id: string; title: string };
export type DataView = 'vacancies' | 'sources';

export function EditorData({ view, onSessionExpired }: { view: DataView; onSessionExpired: () => void }) {
  const [profiles, setProfiles] = useState<Profile[] | null>(null);
  const [profession, setProfession] = useState('');
  const [loadError, setLoadError] = useState('');
  const [reload, setReload] = useState(0);
  const [collectionRefresh, setCollectionRefresh] = useState(0);
  const refreshMaterials = useCallback(() => setCollectionRefresh(value => value + 1), []);

  useEffect(() => {
    const controller = new AbortController();
    const { signal } = controller;
    setLoadError('');
    api<Profile[]>('/discovery/profiles', { signal }).then(result => {
      if (signal.aborted) return;
      setProfiles(result);
      setProfession(current => result.some(profile => profile.id === current) ? current :
        result.find(profile => profile.id === 'python_backend')?.id ?? result[0]?.id ?? '');
    }).catch(error => {
      if (signal.aborted) return;
      if (error instanceof ApiError && error.status === 401) onSessionExpired();
      else setLoadError(describe(error));
    });
    return () => controller.abort();
  }, [reload, onSessionExpired]);

  function profileSaved(profile: Profile) {
    setProfiles(current => [...(current ?? []).filter(item => item.id !== profile.id), profile]);
    setProfession(current => current === profession ? profile.id : current);
    setReload(value => value + 1);
  }

  return <section className="stack">
    <h1>Данные по профессиям</h1>
    <div className="panel">
      <label htmlFor="data-profession">Профессия</label>
      <select id="data-profession" value={profession} disabled={!profiles?.length}
        onChange={event => setProfession(event.target.value)}>
        {!profiles?.length && <option value="">{profiles ? 'Нет профессий' : 'Загружаем профессии'}</option>}
        {profiles?.map(profile => <option key={profile.id} value={profile.id}>{profile.title || profile.id}</option>)}
      </select>
      {loadError && <><p role="alert">{loadError}</p>
        <button type="button" className="secondary" onClick={() => setReload(value => value + 1)}>Повторить загрузку</button>
      </>}
    </div>
    <nav className="actions data-nav" aria-label="Данные по профессии">
      <a href="#/data/vacancies" aria-current={view === 'vacancies' ? 'page' : undefined}>Вакансии HH</a>
      <a href="#/data/sources" aria-current={view === 'sources' ? 'page' : undefined}>Источники</a>
    </nav>
    {profession && <ProfessionCollection key={profession} profession={profession}
      onSessionExpired={onSessionExpired} onCompleted={refreshMaterials}/>}
    <div hidden={view !== 'vacancies'}>
      <Vacancies key={profession} profile={profession} collectionRefresh={collectionRefresh}
        onProfileSaved={profileSaved} onSessionExpired={onSessionExpired}/>
    </div>
    <div hidden={view !== 'sources'}>
      {profession ? <Discovery key={profession} profession={profession} collectionRefresh={collectionRefresh}
        onSessionExpired={onSessionExpired}/> : profiles &&
        <p>Добавьте профессию на вкладке «Вакансии HH», чтобы собирать источники.</p>}
    </div>
  </section>;
}
