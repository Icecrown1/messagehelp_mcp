import asyncio
import os
import time
from datetime import datetime, timedelta

import fakeredis.aioredis
import httpx
import pytest

from threads_client import ThreadsAPI
from threads_growth import PolicyBlocked, TZ
from threads_module import ThreadsModule
from threads_store import RevisionConflict, ThreadsStore


class API:
    calls = []
    count = 10
    changed = False

    def __init__(self, token):
        pass

    async def identity(self, expected_id=''):
        return {'id': '1234', 'username': 'astro_orbi'}

    async def public_post(self, pid):
        return {'id': pid, 'username': 'reader' + pid, 'text': 'Изменено' if self.changed else 'Тема',
                'permalink': 'https://www.threads.com/@reader/post/' + pid, 'is_reply': False}

    async def search(self, query, search_type, after):
        return {'data': [await self.public_post(str(i)) for i in range(100, 110)], 'after': None}

    async def insights(self, pid):
        return {'data': [{'name': 'views', 'values': [{'value': self.count}]},
                         {'name': 'replies', 'values': [{'value': 2}]}], 'after': None}

    async def create_text(self, text, user_id, reply_to_id=None):
        self.calls.append(('create', reply_to_id))
        return '5678'

    async def publish(self, container_id, user_id):
        self.calls.append(('publish',))
        return '9012'


@pytest.fixture
def module(monkeypatch):
    monkeypatch.setenv('THREADS_AUTONOMY_ENABLED', 'true')
    monkeypatch.setenv('THREADS_EXTERNAL_REPLIES_ENABLED', 'true')
    monkeypatch.setattr(time, 'time', lambda: datetime(2026, 11, 8, 8, tzinfo=TZ).timestamp())
    API.calls, API.count, API.changed = [], 10, False
    return ThreadsModule(ThreadsStore(fakeredis.aioredis.FakeRedis(decode_responses=True), 'test-only'),
                         'test-only', True, API)


def at(hour, minute=0, day=8):
    return datetime(2026, 11, day, hour, minute, tzinfo=TZ).isoformat()


async def auto(module, text, when):
    d = await module.save_draft(text, when)
    return await module.growth.queue(d['id'], d['revision'], 'icecrown1')


@pytest.mark.asyncio
async def test_persistent_daily_limit_and_concurrent_approval(module):
    await auto(module, 'Один', at(10))
    d2 = await module.save_draft('Два', at(12))
    d3 = await module.save_draft('Три', at(14))
    outcomes = await asyncio.gather(*[module.growth.queue(d['id'], d['revision'], 'owner')
                                      for d in (d2, d3)], return_exceptions=True)
    assert sum(isinstance(v, dict) for v in outcomes) == 1
    assert all(isinstance(v, (dict, PolicyBlocked, RevisionConflict)) for v in outcomes)
    other = ThreadsModule(module.store, 'test-only', True, API)
    with pytest.raises(PolicyBlocked):
        await auto(other, 'Четыре', at(16))


@pytest.mark.asyncio
async def test_disabled_autonomy_and_time_windows(module, monkeypatch):
    monkeypatch.setenv('THREADS_AUTONOMY_ENABLED', 'false')
    with pytest.raises(PolicyBlocked):
        await auto(module, 'Текст', at(10))
    monkeypatch.setenv('THREADS_AUTONOMY_ENABLED', 'true')
    for when in (at(22), at(8, 30), at(10, day=11)):
        with pytest.raises(PolicyBlocked):
            await auto(module, 'Текст', when)


@pytest.mark.asyncio
async def test_total_post_cap_counts_existing_manual_queue(module):
    for i in range(8):
        d = await module.save_draft('Базовый ' + str(i), at(9+i))
        await module.approve(d['id'], d['revision'], 'owner')
    with pytest.raises(PolicyBlocked, match='суточный'):
        await auto(module, 'Дополнительный', at(20))


@pytest.mark.asyncio
async def test_gap_duplicate_and_edit_revoke_autonomy(module):
    a = await auto(module, 'Тема дня', at(10))
    with pytest.raises(PolicyBlocked, match='интервал'):
        await auto(module, 'Другая тема', at(10, 20))
    with pytest.raises(PolicyBlocked, match='текст'):
        await auto(module, '  ТЕМА  дня  ', at(12))
    edited = await module.save_draft('Изменённый', at(12), a['id'], a['revision'])
    assert edited['status'] == 'draft'
    assert not await module.store.due(datetime(2026, 11, 8, 20, tzinfo=TZ).timestamp())
    with pytest.raises(RevisionConflict):
        await module.growth.queue(a['id'], a['revision'], 'owner')


@pytest.mark.asyncio
async def test_external_provenance_target_and_caps(module):
    with pytest.raises(ValueError):
        await module.growth.save_external_reply('999', 'Ответ', at(10))
    await module.growth.search('таро')
    for i in range(5):
        d = await module.growth.save_external_reply(str(100+i), 'Ответ ' + str(i), at(10+i))
        await module.growth.queue(d['id'], d['revision'], 'owner')
    sixth = await module.growth.save_external_reply('105', 'Шестой ответ', at(16))
    with pytest.raises(PolicyBlocked, match='суточный'):
        await module.growth.queue(sixth['id'], sixth['revision'], 'owner')
    repeat = await module.growth.save_external_reply('100', 'Повторный ответ', at(16, day=9))
    with pytest.raises(PolicyBlocked, match='ветку'):
        await module.growth.queue(repeat['id'], repeat['revision'], 'owner')
    with pytest.raises(PermissionError):
        await module.approve(sixth['id'], sixth['revision'], 'owner')


@pytest.mark.asyncio
async def test_one_author_per_day_and_external_flag(module, monkeypatch):
    await module.growth.search('таро')
    a = await module.growth.save_external_reply('100', 'Ответ', at(10))
    await module.growth.queue(a['id'], a['revision'], 'owner')
    b = await module.growth.save_external_reply('101', 'Другой ответ', at(12))
    b['reply_target']['username'] = 'reader100'
    with pytest.raises(PolicyBlocked, match='автору'):
        module.growth.guard(b, (await module.store.list())['data'])
    monkeypatch.setenv('THREADS_EXTERNAL_REPLIES_ENABLED', 'false')
    with pytest.raises(PolicyBlocked):
        await module.growth.queue(b['id'], b['revision'], 'owner')


@pytest.mark.asyncio
async def test_worker_rechecks_flags_and_external_target(module, monkeypatch):
    a = await auto(module, 'Ситуативный', at(10))
    monkeypatch.setenv('THREADS_AUTONOMY_ENABLED', 'false')
    monkeypatch.setattr(time, 'time', lambda: a['publish_at_ts']+1)
    await module.run_due()
    assert (await module.get_draft(a['id']))['status'] == 'cancelled'
    assert API.calls == []
    monkeypatch.setenv('THREADS_AUTONOMY_ENABLED', 'true')
    await module.growth.search('таро')
    b = await module.growth.save_external_reply('100', 'Ответ', at(12))
    b = await module.growth.queue(b['id'], b['revision'], 'owner')
    API.changed = True
    monkeypatch.setattr(time, 'time', lambda: b['publish_at_ts']+1)
    await module.run_due()
    assert (await module.get_draft(b['id']))['status'] == 'needs_review'
    assert API.calls == []


@pytest.mark.asyncio
async def test_actual_send_spacing_after_delay(module, monkeypatch):
    a = await auto(module, 'Один', at(10))
    b = await auto(module, 'Два', at(11))
    monkeypatch.setattr(time, 'time', lambda: a['publish_at_ts']+30*60)
    await module.run_due()
    monkeypatch.setattr(time, 'time', lambda: b['publish_at_ts']+1)
    await module.run_due()
    assert API.calls == [('create', None), ('publish',)]
    assert (await module.get_draft(b['id']))['status'] == 'cancelled'


@pytest.mark.asyncio
async def test_insights_store_previous_day_not_same_day_and_encrypt(module, monkeypatch):
    a = await module.growth.insights('200')
    assert a['previous_observation'] is None
    API.count = 15
    same_day = await module.growth.insights('200')
    assert same_day['previous_observation'] is None and same_day['history_days'] == 1
    monkeypatch.setattr(time, 'time', lambda: datetime(2026, 11, 9, 8, tzinfo=TZ).timestamp())
    API.count = 24
    b = await module.growth.insights('200')
    assert b['delta_since_previous_observation']['views'] == 9
    assert b['history_days'] == 2
    assert 'views' not in await module.store.redis.get(module.store.prefix+'insights:200')


@pytest.mark.asyncio
async def test_search_request_is_read_only_and_validated():
    def handler(request):
        assert request.method == 'GET'
        assert request.url.path == '/v1.0/keyword_search'
        assert request.url.params['q'] == 'таро'
        assert request.url.params['search_type'] == 'RECENT'
        return httpx.Response(200, json={'data': []})
    api = ThreadsAPI('test', httpx.MockTransport(handler))
    assert await api.search('таро') == {'data': [], 'after': None}
    with pytest.raises(ValueError):
        await api.search('x')
