import { useCallback, useEffect, useState } from 'react';
import { api, ApiError, describe } from './api';
import { Discovery, type Provider } from './Discovery';
import { CollectionOverview, EscoPanel, useCollectionStatus } from './ProfessionCollection';
import { Vacancies } from './Vacancies';

type Profile = { id: string; title: string };
type DataTab = 'overview' | 'vacancies' | 'esco' | Provider;
const tabs: [DataTab, string][] = [
  ['overview', 'Обзор'],
  ['vacancies', 'Вакансии'],
  ['esco', 'ESCO'],
  ['github', 'GitHub Issues'],
  ['trends', 'GitHub Trends'],
  ['docs', 'Документация'],
  ['openalex', 'Научные статьи'],
  ['skills', 'Agent Skills'],
];
const discoveryTabs: readonly DataTab[] = ['github', 'trends', 'docs', 'openalex', 'skills'];

export function EditorData({ onSessionExpired }: { onSessionExpired: () => void }) {
  const [profiles, setProfiles] = useState<Profile[] | null>(null);
  const [profession, setProfession] = useState('');
  const [tab, setTab] = useState<DataTab>('overview');
  const [loadError, setLoadError] = useState('');
  const [reload, setReload] = useState(0);
  const [collectionRefresh, setCollectionRefresh] = useState(0);
  const refreshMaterials = useCallback(() => setCollectionRefresh(value => value + 1), []);
  const collection = useCollectionStatus(profession, onSessionExpired, refreshMaterials);

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
    {profession && <div className="data-layout">
      <nav className="panel source-menu" aria-label="Источники данных">
        {tabs.map(([id, label]) => <button key={id} type="button" className="quiet"
          aria-current={tab === id ? 'page' : undefined} onClick={() => setTab(id)}>{label}</button>)}
      </nav>
      <div className="stack">
        {tab === 'overview' && <CollectionOverview status={collection}/>}
        {tab === 'vacancies' && <Vacancies key={profession} profile={profession}
          collectionRefresh={collectionRefresh} onProfileSaved={profileSaved} onSessionExpired={onSessionExpired}/>}
        {tab === 'esco' && (collection.quality?.esco ? <div className="panel">
            <EscoPanel esco={collection.quality.esco}
              path={'/discovery/collection/' + encodeURIComponent(profession) + '/esco'}
              busy={collection.starting || !!collection.run?.running}
              onCollect={() => void collection.start(undefined, true)} onSaved={collection.refreshQuality}
              onSessionExpired={onSessionExpired}/>
          </div> : <p role="status">Загружаем настройки ESCO…</p>)}
        {discoveryTabs.includes(tab) && <Discovery key={`${profession}:${tab}`} profession={profession}
          provider={tab as Provider} collectionRefresh={collectionRefresh} onSessionExpired={onSessionExpired}/>}
      </div>
    </div>}
  </section>;
}
