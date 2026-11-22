import { useCallback, useEffect, useState } from 'react';
import { api, ApiError, describe } from './api';
import { Discovery, type Provider } from './Discovery';
import { OnetTasks } from './OnetTasks';
import { CollectionOverview, EscoPanel, useCollectionStatus } from './ProfessionCollection';
import { Vacancies } from './Vacancies';

type Profile = { id: string; title: string };
type DataTab = 'overview' | 'vacancies' | 'esco' | 'onet' | 'sources' | Provider;
const tabs: [DataTab, string][] = [
  ['overview', 'Обзор'],
  ['vacancies', 'Вакансии'],
  ['esco', 'ESCO'],
  ['onet', 'O*NET'],
  ['github', 'GitHub Issues'],
  ['trends', 'GitHub Trends'],
  ['docs', 'Документация'],
  ['openalex', 'Научные статьи'],
  ['skills', 'Agent Skills'],
  ['habr', 'Хабр'],
  ['sources', 'Все материалы'],
];
const discoveryTabs: readonly Provider[] = ['github', 'trends', 'docs', 'openalex', 'skills', 'habr'];

function tabRoute(tab: DataTab) {
  return tab === 'overview' ? '#/data' : '#/data/' + tab;
}

export function dataTabForRoute(route: string): DataTab | undefined {
  if (route === '#/vacancies') return 'vacancies';
  if (route === '#/discovery') return 'sources';
  return tabs.find(([id]) => tabRoute(id) === route)?.[0];
}

export function EditorData({ view, onSessionExpired }: { view: DataTab; onSessionExpired: () => void }) {
  const [profiles, setProfiles] = useState<Profile[] | null>(null);
  const [profession, setProfession] = useState('');
  const [loadError, setLoadError] = useState('');
  const [reload, setReload] = useState(0);

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
    <div className="data-layout">
      <nav className="panel source-menu" aria-label="Источники данных">
        {tabs.map(([id, label]) => <a key={id} href={tabRoute(id)} className="quiet"
          aria-current={view === id ? 'page' : undefined}>{label}</a>)}
      </nav>
      <ProfessionData key={profession} profession={profession} view={view} profilesLoaded={profiles !== null}
        onProfileSaved={profileSaved} onSessionExpired={onSessionExpired}/>
    </div>
  </section>;
}

function ProfessionData({ profession, view, profilesLoaded, onProfileSaved, onSessionExpired }: {
  profession: string; view: DataTab; profilesLoaded: boolean;
  onProfileSaved: (profile: Profile) => void; onSessionExpired: () => void;
}) {
  const [collectionRefresh, setCollectionRefresh] = useState(0);
  const refreshMaterials = useCallback(() => setCollectionRefresh(value => value + 1), []);
  const collection = useCollectionStatus(profession, onSessionExpired, refreshMaterials);
  const provider = discoveryTabs.find(id => id === view);

  return <div className="stack">
    {profession && (view === 'overview' || view === 'vacancies') &&
      <CollectionOverview status={collection} vacanciesOnly={view === 'vacancies'}/>}
    <div hidden={view !== 'vacancies'}>
      <Vacancies profile={profession} visible={view === 'vacancies'} collectionRefresh={collectionRefresh}
        onProfileSaved={onProfileSaved} onSessionExpired={onSessionExpired}/>
    </div>
    {!profession && profilesLoaded && view !== 'vacancies' &&
      <p>Добавьте профессию на вкладке «Вакансии», чтобы собирать источники.</p>}
    {profession && view === 'esco' && (collection.quality?.esco ? <div className="panel">
      {collection.startError && <p role="alert">{collection.startError}</p>}
      <EscoPanel esco={collection.quality.esco}
        path={'/discovery/collection/' + encodeURIComponent(profession) + '/esco'}
        busy={collection.starting || !!collection.run?.running}
        onCollect={() => void collection.start(undefined, 'esco')} onSaved={collection.refreshQuality}
        onSessionExpired={onSessionExpired}/>
    </div> : collection.loadError ? <p role="alert">{collection.loadError}</p> :
      <p role="status">Загружаем настройки ESCO…</p>)}
    {profession && view === 'onet' && <OnetTasks profession={profession} onSessionExpired={onSessionExpired}/>}
    {profession && (provider || view === 'sources') && <Discovery key={view} profession={profession}
      provider={provider} collectionRefresh={collectionRefresh} onSessionExpired={onSessionExpired}/>}
  </div>;
}
