import asyncio
import time
from datetime import datetime, timezone
from types import SimpleNamespace

import fakeredis.aioredis
import httpx
import pytest

from server import build
from threads_client import ThreadsAPI, ThreadsError
from threads_module import ThreadsModule, text_value
from threads_store import RevisionConflict, ThreadsStore


class FakeAPI:
    def __init__(self, token, calls, fail=False, username='astro_orbi'):
        self.token, self.calls, self.fail, self.username = token, calls, fail, username

    async def identity(self, expected_id=''):
        if self.username != 'astro_orbi' or expected_id not in ('', '1234'):
            raise ThreadsError('wrong account')
        return {'id': '1234', 'username': self.username}

    async def create_text(self, text, user_id, reply_to_id=None):
        self.calls.append(('create', text, user_id) if reply_to_id is None else
                          ('reply', text, user_id, reply_to_id))
        await asyncio.sleep(0)
        return '5678'

    async def publish(self, container_id, user_id):
        self.calls.append(('publish', container_id, user_id))
        if self.fail:
            raise ThreadsError('timeout')
        return '9012'

    async def refresh(self):
        self.calls.append(('refresh',))
        return {'access_token': 'rotated-secret-test-only', 'expires_in': 5184000}


@pytest.fixture
def queue():
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    calls = []
    store = ThreadsStore(redis, 'test-signing-key-only')
    factory = lambda token: FakeAPI(token, calls)
    return ThreadsModule(store, 'initial-secret-test-only', True, factory), calls


def future(seconds=120):
    return datetime.fromtimestamp(time.time() + seconds, timezone.utc).isoformat()


async def queued(module):
    draft = await module.save_draft('Таро: вопрос к себе, а не готовый приговор.', future())
    return await module.approve(draft['id'], draft['revision'], 'icecrown1')


@pytest.mark.asyncio
async def test_draft_never_publishes_without_approval(queue, monkeypatch):
    module, calls = queue
    draft = await module.save_draft('Только черновик.', future())
    monkeypatch.setattr(time, 'time', lambda: draft['publish_at_ts'] + 5)
    await module.run_due()
    assert calls == []
    assert (await module.get_draft(draft['id']))['status'] == 'draft'


@pytest.mark.asyncio
async def test_revision_edit_revokes_approval_and_cancel_removes_due(queue, monkeypatch):
    module, calls = queue
    draft = await queued(module)
    edited = await module.save_draft('Изменённый текст.', future(), draft['id'], draft['revision'])
    assert edited['status'] == 'draft' and edited['approved_by'] is None
    with pytest.raises(RevisionConflict):
        await module.approve(draft['id'], draft['revision'], 'icecrown1')
    approved = await module.approve(edited['id'], edited['revision'], 'icecrown1')
    cancelled = await module.cancel(approved['id'], approved['revision'])
    monkeypatch.setattr(time, 'time', lambda: draft['publish_at_ts'] + 5)
    await module.run_due()
    assert cancelled['status'] == 'cancelled' and calls == []


@pytest.mark.asyncio
async def test_restart_and_two_workers_publish_exactly_once(queue, monkeypatch):
    first, calls = queue
    draft = await queued(first)
    second = ThreadsModule(ThreadsStore(first.store.redis, 'test-signing-key-only'),
                           first.seed_token, True, first.api_factory)
    monkeypatch.setattr(time, 'time', lambda: draft['publish_at_ts'] + 2)
    await asyncio.gather(first.run_due(), second.run_due())
    await second.run_due()
    result = await second.get_draft(draft['id'])
    assert result['status'] == 'published' and result['post_id'] == '9012'
    assert calls == [('create', draft['text'], '1234'), ('publish', '5678', '1234')]
    for key in await first.store.redis.keys(first.store.prefix + '*'):
        if await first.store.redis.type(key) == 'string':
            raw = await first.store.redis.get(key)
            assert draft['text'] not in raw and first.seed_token not in raw


@pytest.mark.asyncio
async def test_lost_publish_response_is_not_retried(queue, monkeypatch):
    module, calls = queue
    module.api_factory = lambda token: FakeAPI(token, calls, fail=True)
    draft = await queued(module)
    monkeypatch.setattr(time, 'time', lambda: draft['publish_at_ts'] + 2)
    await module.run_due()
    await module.run_due()
    result = await module.get_draft(draft['id'])
    assert result['status'] == 'needs_review' and result['container_id'] == '5678'
    assert len(calls) == 2
    with pytest.raises(ValueError):
        await module.save_draft('Новый текст', future(), draft['id'], result['revision'])


@pytest.mark.asyncio
async def test_crashed_inflight_becomes_review_without_retry(queue, monkeypatch):
    module, calls = queue
    draft = await queued(module)
    claimed = await module.store.put({**draft, 'status': 'publishing'}, draft['revision'])
    monkeypatch.setattr(time, 'time', lambda: claimed['updated_at'] + 601)
    await module.run_due()
    assert (await module.get_draft(draft['id']))['status'] == 'needs_review'
    assert calls == []


@pytest.mark.asyncio
async def test_late_post_requires_new_schedule(queue, monkeypatch):
    module, calls = queue
    draft = await queued(module)
    monkeypatch.setattr(time, 'time', lambda: draft['publish_at_ts'] + 3601)
    await module.run_due()
    assert (await module.get_draft(draft['id']))['status'] == 'missed'
    assert calls == []


@pytest.mark.asyncio
async def test_publishing_flag_and_account_binding(queue, monkeypatch):
    module, calls = queue
    draft = await module.save_draft('Текст', future())
    module.publishing = False
    with pytest.raises(PermissionError):
        await module.approve(draft['id'], draft['revision'], 'icecrown1')
    module.publishing = True
    draft = await module.approve(draft['id'], draft['revision'], 'icecrown1')
    module.api_factory = lambda token: FakeAPI(token, calls, username='another-account')
    monkeypatch.setattr(time, 'time', lambda: draft['publish_at_ts'] + 2)
    await module.run_due()
    assert (await module.get_draft(draft['id']))['status'] == 'needs_review'
    assert calls == []


@pytest.mark.asyncio
async def test_refresh_persists_encrypted_token_across_restart(queue):
    module, calls = queue
    await module.verified_access()
    auth = await module.store.get('auth')
    await module.store.put({**auth, 'refreshed_at': time.time() - 8 * 86400}, auth['revision'])
    await module.refresh_access()
    restarted = ThreadsModule(module.store, module.seed_token, True, module.api_factory)
    api, auth = await restarted.verified_access()
    assert api.token == 'rotated-secret-test-only'
    assert auth['expires_at'] > time.time() and calls == [('refresh',)]
    assert 'rotated-secret' not in await module.store.redis.get(module.store.key('auth'))
    assert 'token' not in await module.status()


@pytest.mark.asyncio
async def test_auth_record_is_inaccessible_as_draft(queue):
    module, _ = queue
    await module.verified_access()
    for invalid in ['auth', '../auth', 'due', 'index']:
        with pytest.raises(ValueError):
            await module.get_draft(invalid)
        with pytest.raises(ValueError):
            await module.save_draft('text', future(), invalid, 'anything')
    assert (await module.store.list())['data'] == []


@pytest.mark.asyncio
@pytest.mark.parametrize('scheduled', ['2026-11-05T10:00:00', 'bad', '2001-01-01T00:00:00Z'])
async def test_ambiguous_or_expired_schedule_rejected(queue, scheduled):
    with pytest.raises(ValueError):
        await queue[0].save_draft('Текст', scheduled)


@pytest.mark.parametrize('text', ['', '  ', 'a' * 501, '🤍' * 251, 'bad\x00text'])
def test_invalid_text_rejected(text):
    with pytest.raises(ValueError):
        text_value(text)


@pytest.mark.asyncio
async def test_api_uses_fixed_host_bearer_and_no_auto_publish():
    seen = []
    def handler(request):
        seen.append(request)
        assert request.url.host == 'graph.threads.net'
        assert request.headers['Authorization'] == 'Bearer private-test-token'
        assert 'private-test-token' not in str(request.url)
        if request.url.path.endswith('/me'):
            return httpx.Response(200, json={'id': '1234', 'username': 'astro_orbi'})
        return httpx.Response(200, json={'id': '5678'})
    api = ThreadsAPI('private-test-token', transport=httpx.MockTransport(handler))
    assert (await api.identity())['id'] == '1234'
    assert await api.create_text('hello', '1234') == '5678'
    assert 'auto_publish_text' not in seen[-1].content.decode()
    assert seen[-1].method == 'POST'


@pytest.mark.asyncio
async def test_http_error_and_paging_do_not_leak_tokens():
    api = ThreadsAPI('private-test-token', transport=httpx.MockTransport(
        lambda request: httpx.Response(403, json={'error': 'private-test-token'})))
    with pytest.raises(ThreadsError) as err:
        await api.posts()
    assert 'private-test-token' not in str(err.value)
    api.transport = httpx.MockTransport(lambda request: httpx.Response(200, json={
        'data': [], 'paging': {'next': 'https://graph.threads.net/?access_token=SECRET',
                               'cursors': {'after': 'cursor'}}}))
    assert await api.posts() == {'data': [], 'after': 'cursor'}


@pytest.mark.asyncio
async def test_reply_preserves_target_and_approval(queue, monkeypatch):
    module, calls = queue

    async def target(self, post_id, comment_id):
        return {'post_id': post_id, 'id': comment_id, 'username': 'reader', 'text': 'Вопрос?'}

    monkeypatch.setattr(FakeAPI, 'reply_target', target, raising=False)
    draft = await module.save_draft('Ответ.', future(), post_id='100', reply_to_id='200')
    assert draft['reply_target']['username'] == 'reader'
    approved = await module.approve(draft['id'], draft['revision'], 'icecrown1')
    edited = await module.save_draft('Другой ответ.', future(), draft['id'], approved['revision'])
    assert edited['status'] == 'draft' and edited['reply_target'] == draft['reply_target']
    with pytest.raises(RevisionConflict):
        await module.approve(draft['id'], approved['revision'], 'icecrown1')
    with pytest.raises(ValueError):
        await module.save_draft('Текст', future(), draft['id'], edited['revision'],
                                post_id='100', reply_to_id='201')
    with pytest.raises(ValueError):
        await module.save_draft('Текст', future(), post_id='100')
    approved = await module.approve(draft['id'], edited['revision'], 'icecrown1')
    monkeypatch.setattr(time, 'time', lambda: approved['publish_at_ts'] + 1)
    await module.run_due()
    await module.run_due()
    assert calls == [('reply', 'Другой ответ.', '1234', '200'), ('publish', '5678', '1234')]


@pytest.mark.asyncio
async def test_reply_verifies_conversation_and_uses_only_the_selected_id():
    from urllib.parse import parse_qs
    def handler(request):
        if request.method == 'POST':
            assert parse_qs(request.content.decode()) == {
                'text': ['Ответ'], 'media_type': ['TEXT'], 'reply_to_id': ['200']}
            return httpx.Response(200, json={'id': '300'})
        if request.url.path.endswith('/conversation'):
            if 'after' in request.url.params:
                return httpx.Response(200, json={'data': [{'id': '200', 'username': 'reader'}]})
            return httpx.Response(200, json={'data': [],
                'paging': {'next': 'unused', 'cursors': {'after': 'page2'}}})
        return httpx.Response(200, json={'id': '100', 'username': 'astro_orbi'})

    api = ThreadsAPI('test', transport=httpx.MockTransport(handler))
    assert (await api.reply_target('100', '200'))['username'] == 'reader'
    with pytest.raises(ThreadsError):
        await api.reply_target('100', '201')
    assert await api.create_text('Ответ', '1234', '200') == '300'


@pytest.mark.asyncio
async def test_wrong_profile_or_other_owners_post_rejected():
    api = ThreadsAPI('private-test-token', transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={'id': '1234', 'username': 'another'})))
    with pytest.raises(ThreadsError):
        await api.identity()
    with pytest.raises(ThreadsError):
        await api.replies('1234')


@pytest.fixture
def server_env(monkeypatch):
    monkeypatch.setenv('GITHUB_CLIENT_ID', 'test-id')
    monkeypatch.setenv('GITHUB_CLIENT_SECRET', 'test-secret')
    monkeypatch.setenv('FASTMCP_SERVER_AUTH_OAUTH_PROXY_JWT_SIGNING_KEY', 'test-signing-key-only')
    monkeypatch.setenv('BASE_URL', 'https://test.example')
    monkeypatch.delenv('OAUTH_REDIS_URL', raising=False)
    monkeypatch.delenv('RENDER_EXTERNAL_URL', raising=False)
    monkeypatch.delenv('ENABLE_THREADS', raising=False)
    monkeypatch.delenv('THREADS_ACCESS_TOKEN', raising=False)
    monkeypatch.delenv('THREADS_ENABLE_PUBLISHING', raising=False)


@pytest.mark.asyncio
async def test_disabled_module_preserves_original_toolset(server_env):
    assert set(await build().get_tools()) == {'get_template', 'list_templates', 'preview_changes', 'update_template'}


@pytest.mark.asyncio
async def test_all_threads_tools_require_allowed_mcp_identity(server_env, monkeypatch):
    import inspect
    monkeypatch.setenv('ENABLE_THREADS', 'true')
    monkeypatch.setenv('THREADS_REDIS_URL', 'redis://test:6379')
    monkeypatch.setattr('threads_module.Redis.from_url', lambda *a, **kw:
                        fakeredis.aioredis.FakeRedis(decode_responses=True))
    monkeypatch.setattr('server.get_access_token', lambda: None)
    tools = await build().get_tools()
    threads = {name: tool for name, tool in tools.items() if name.startswith('threads_')}
    assert len(threads) == 13
    for tool in threads.values():
        args = {p.name: '' for p in inspect.signature(tool.fn).parameters.values()
                if p.default is inspect.Parameter.empty}
        with pytest.raises(PermissionError):
            await tool.fn(**args)
    monkeypatch.setattr('server.get_access_token', lambda: SimpleNamespace(claims={'login': 'not-owner'}))
    with pytest.raises(PermissionError):
        await threads['threads_status'].fn()
    monkeypatch.setattr('server.get_access_token', lambda: SimpleNamespace(claims={'login': 'Icecrown1'}))
    assert (await threads['threads_status'].fn())['connected'] is False
    with pytest.raises(ValueError):
        await threads['threads_preview_draft'].fn(draft_id='auth')


def test_enabled_module_requires_persistent_store(server_env, monkeypatch):
    monkeypatch.setenv('ENABLE_THREADS', 'true')
    monkeypatch.delenv('THREADS_REDIS_URL', raising=False)
    with pytest.raises(RuntimeError, match='Redis'):
        build()


@pytest.mark.asyncio
async def test_enabled_module_lifespan_keeps_health_and_auth(server_env, monkeypatch):
    monkeypatch.setenv('ENABLE_THREADS', 'true')
    monkeypatch.setenv('THREADS_REDIS_URL', 'redis://test:6379')
    monkeypatch.setattr('threads_module.Redis.from_url', lambda *a, **kw:
                        fakeredis.aioredis.FakeRedis(decode_responses=True))
    app = build().http_app(stateless_http=True)
    async with app.lifespan(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                    base_url='https://test.example') as client:
            assert (await client.get('/health')).status_code == 200
            assert (await client.post('/mcp', json={'method': 'tools/list'})).status_code == 401
