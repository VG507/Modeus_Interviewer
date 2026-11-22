import socket
from datetime import timezone
from email.utils import format_datetime

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from backend import habr, profession_collection
from backend.app import create_app
from backend.auth import current_user
from backend.db import Base, DiscoveryCandidate, DiscoveryItem, DiscoveryRevision, VacancyCollection, now
from backend.discovery_fetch import CollectionError
from backend.settings import Settings
from backend.source_context import discovery_sources_current, sources_for_followup


PROBLEM = "Запрос PostgreSQL выполнялся медленно из-за последовательного сканирования."
ACTION = "Команда добавила составной индекс."
VERIFICATION = "Сравнили планы выполнения на одном наборе данных."
TASK = "Оптимизация медленного запроса PostgreSQL"


def rss(*items):
    return ('<rss xmlns:dc="http://purl.org/dc/elements/1.1/"><channel>' + ''.join(items) + '</channel></rss>').encode()


def article(number, title, text='', rating=0, *, url=None):
    date = format_datetime(now().replace(tzinfo=timezone.utc))
    url = url or f'https://habr.com/ru/companies/example/articles/{number}/?utm_source=rss'
    return f'''<item><title>{title}</title><link>{url}</link><pubDate>{date}</pubDate>
    <dc:creator>editor</dc:creator><rating>{rating}</rating><description><![CDATA[{text}]]></description></item>'''


def test_habr_rss_and_profession_filters():
    roles = {'python_backend': 'FastAPI', 'frontend': 'Web Vitals', 'qa': 'Playwright',
             'devops': 'Kubernetes', 'ml_engineer': 'Дрейф данных'}
    entries = habr.read_feed(rss(
        *(article(number, title, '<p>Текст</p><script>скрытый текст</script>')
          for number, title in enumerate(roles.values(), 1)),
        article(1, 'Дубликат', url='https://habr.com/ru/articles/1/'),
        article(6, 'Другая площадка', url='https://habr.com.example/ru/articles/6/'),
        article(8, 'Новость', url='https://habr.com/ru/news/8/'),
    ), now())
    assert len(entries) == len(roles)
    for entry, (profession, title) in zip(entries, roles.items()):
        assert entry.title == title and entry.payload['author'] == 'editor'
        assert '?' not in entry.url and entry.payload['fragments']['rss']['text'] == 'Текст'
        assert [role for role, pattern in habr.PROFILE_TOPICS.items() if habr.matches_profile(entry, pattern)] == [profession]
    with pytest.raises(CollectionError):
        habr.read_feed(b'<!DOCTYPE rss [<!ENTITY bad "x">]><rss><channel>&bad;</channel></rss>', now())


def test_habr_collection_retry_sources_and_interview(database_url, monkeypatch):
    settings = Settings(_env_file=None, database_url=database_url, llm_base_url='http://localhost:8001/v1', llm_model='test')
    app = create_app(settings)
    sessions = app.state.sessions
    Base.metadata.create_all(app.state.engine)
    ml_id = '1' * 32
    with sessions.begin() as db:
        db.add_all([
            VacancyCollection(profession_id='python_backend', title='Python Backend', title_key='python backend', approved=True),
            VacancyCollection(profession_id=ml_id, title='ML инженер', title_key='ml инженер'),
        ])
    app.dependency_overrides[current_user] = lambda: {'id': 'editor', 'is_editor': True}
    headers = {'Origin': app.state.app_origin}
    path = '/api/v1/discovery/collection/python_backend'
    text = f'<p>{PROBLEM}</p><p>{ACTION}</p><p>{VERIFICATION}</p>'
    article_1 = article(1, 'Оптимизация PostgreSQL', text, rating=20)
    article_2 = article(2, 'Новости PostgreSQL', 'PostgreSQL обновился.')
    article_3 = article(3, 'MLOps и дрейф данных', 'Публикация про ML-модели.')
    feed = rss(article_1, article_2, article_3)
    unreachable = True
    calls = []

    def feed_response(request):
        assert str(request.url) == habr.FEED
        if unreachable:
            raise httpx.ConnectError('DNS failure', request=request) from socket.gaierror('DNS failure')
        return httpx.Response(200, content=feed)

    def refresh(_, settings, profile, source):
        if source == 'habr':
            return habr.collect_habr(sessions, settings, profile, transport=httpx.MockTransport(feed_response))
        return {'status': 'skipped'}

    async def analyze(settings, name, schema, instructions, payload, parse=None):
        calls.append(payload['title'])
        return {'relevant': True, 'task': TASK, 'problem': PROBLEM, 'action': ACTION,
                'verification': VERIFICATION, 'detail': 'последовательного сканирования',
                'technology': 'PostgreSQL', 'conditions': [], 'version': None}

    monkeypatch.setattr(profession_collection, 'refresh_source', refresh)
    monkeypatch.setattr(habr, 'structured_completion', analyze)
    intake = {'role': 'Python Backend'}
    state = {'facts': [{'field': 'task', 'text': 'Ускорял медленный запрос PostgreSQL для отчётов.'}]}
    with TestClient(app) as client:
        assert client.post(path, headers=headers).status_code == 202
        failed = client.get(path).json()['run']
        assert failed['steps']['habr'] == {'status': 'failed', 'code': 'DISCOVERY_DNS_FAILED'}

        unreachable = False
        assert client.post(path, headers=headers, json={'run_id': failed['id'], 'source': 'habr'}).status_code == 202
        status = client.get(path).json()
        assert status['run']['steps']['habr'] == {'status': 'ok', 'collected': 2, 'accepted': 1}
        assert status['quality']['materials']['habr'] == {'accepted': 1, 'observe': 1}
        items = client.get('/api/v1/discovery?profession_id=python_backend&provider=habr').json()['items']
        assert {item['id']: item['status'] for item in items} == {'habr:1': 'accepted', 'habr:2': 'observe'}
        detail = client.get('/api/v1/discovery/items/habr:1?profession_id=python_backend').json()
        assert detail['assessment']['verification'] == VERIFICATION
        assert detail['assessment']['quote'] == PROBLEM and detail['payload']['rating'] == 20
        with sessions() as db:
            source, = sources_for_followup(db, intake, state, 'verification')
            assert source['task_excerpt'] == PROBLEM and source['url'] == detail['url'] and source['provider'] == 'habr'
            assert discovery_sources_current(db, [source])

        feed = rss(article_1.replace('<rating>20', '<rating>50'), article_2, article_3)
        assert client.post(path, headers=headers).status_code == 202
        assert len(calls) == 2
        with sessions() as db:
            assert discovery_sources_current(db, [source])
            item = db.get(DiscoveryItem, 'habr:1')
            assert item.digest == detail['digest'] and item.payload['rating'] == 50

        feed = rss(article_1.replace(VERIFICATION, 'Описание результата будет добавлено.'), article_3)
        ml_path = f'/api/v1/discovery/collection/{ml_id}'
        assert client.post(ml_path, headers=headers).status_code == 202
        assert client.get(ml_path).json()['run']['steps']['habr']['collected'] == 1
        with sessions() as db:
            assert db.get(DiscoveryCandidate, ('habr:3', ml_id))
            assert db.get(DiscoveryCandidate, ('habr:1', ml_id)) is None
            assert not discovery_sources_current(db, [source])
            assert sources_for_followup(db, intake, state, 'verification') == []
            assert db.scalar(select(func.count()).select_from(DiscoveryRevision).where(
                DiscoveryRevision.item_id == 'habr:1',
            )) == 2
        assert client.post(path, headers=headers).status_code == 202
        assert client.get('/api/v1/discovery/items/habr:1?profession_id=python_backend').json()['status'] == 'observe'
