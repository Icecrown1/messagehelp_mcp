import time
from datetime import datetime, timezone
from uuid import uuid4

import fakeredis.aioredis
import pytest
import pytest_asyncio

from threads_client import ThreadsError
from threads_module import ThreadsModule
from threads_store import RevisionConflict, ThreadsStore


class API:
    def __init__(self, token, state):
        self.token, self.state = token, state

    async def identity(self, expected_id=''):
        if self.state.get('wrong_profile'):
            raise ThreadsError('wrong profile')
        user_id = {'old-test-token': '111', 'new-test-token': '222', 'third-test-token': '333'}[self.token]
        if expected_id and expected_id != user_id:
            raise ThreadsError('wrong ID')
        return {'id': user_id, 'username': 'astro_orbi'}

    async def posts(self, after=''):
        self.state['pages'].append(after)
        if self.state.get('on_read'):
            await self.state.pop('on_read')()
        return {'data': [self.state['history'][1 if after else 0]],
                'after': None if after else 'second-page'}

    async def create_text(self, text, user_id, reply_to_id=None):
        self.state['sends'].append(('create', user_id))
        return '456'

    async def publish(self, container_id, user_id):
        self.state['sends'].append(('publish', user_id))
        return '789'


@pytest_asyncio.fixture
async def setup():
    store = ThreadsStore(fakeredis.aioredis.FakeRedis(decode_responses=True), 'test-signing-only')
    state = {'pages': [], 'history': [], 'sends': []}
    module = ThreadsModule(store, 'old-test-token', True, lambda token: API(token, state))
    await module.verified_access()
    records = []
    for index, status in enumerate(['published', 'published', 'queued', 'needs_review']):
        ts = time.time() - 10000 + index if status == 'published' else time.time() + 120
        record = await store.put({'id': uuid4().hex, 'created_at': time.time(),
            'account': 'astro_orbi', 'status': status, 'user_id': '111',
            'text': f'Разный исходный текст {index}',
            'approved_by': 'owner', 'approved_at': ts - 100,
            'publish_at_ts': ts, 'publish_at': datetime.fromtimestamp(ts, timezone.utc).isoformat(),
            'post_id': str(100 + index) if status == 'published' else None,
            'published_at': ts if status == 'published' else None}, None)
        records.append(record)
        if status == 'published':
            state['history'].append({'id': str(200 + index), 'text': record['text'],
                'timestamp': record['publish_at'], 'username': 'astro_orbi',
                'permalink': f'https://www.threads.com/@astro_orbi/post/test{index}'})
    module.seed_token = 'new-test-token'
    return module, state, records


@pytest.mark.asyncio
@pytest.mark.parametrize('allowed_old_id', ['', '999'])
async def test_mismatch_stays_blocked_without_exact_operator_opt_in(setup, allowed_old_id):
    module, state, records = setup
    module.rebind_from_user_id = allowed_old_id
    with pytest.raises(ThreadsError):
        await module.verified_access()
    assert (await module.store.get('auth'))['user_id'] == '111'
    assert [await module.store.get(r['id']) for r in records] == records
    assert state['pages'] == []


@pytest.mark.asyncio
async def test_verified_migration_preserves_approvals_history_and_survives_restart(setup):
    module, state, records = setup
    module.rebind_from_user_id = '111'
    api, auth = await module.verified_access()
    assert auth['user_id'] == '222' and api.token == 'new-test-token'
    assert state['pages'] == ['', 'second-page']
    migrated = await module.store.get(records[2]['id'])
    for field in ['text', 'status', 'publish_at', 'publish_at_ts', 'approved_at', 'approved_by']:
        assert migrated[field] == records[2][field]
    assert migrated['user_id'] == '222' and migrated['previous_user_id'] == '111'
    assert migrated['revision'] != records[2]['revision']
    assert (await module.store.due(time.time() + 130)) == [records[2]['id']]
    for record in [records[0], records[1], records[3]]:
        assert await module.store.get(record['id']) == record
    restarted = ThreadsModule(module.store, 'new-test-token', True, module.api_factory)
    assert (await restarted.verified_access())[1] == auth
    status = await restarted.status()
    assert status['identity_rebind']['migrated_drafts'] == 1
    assert status['identity_rebind']['verified_posts'] == 2
    assert 'token' not in status and 'new-test-token' not in str(status)
    assert 'new-test-token' not in await module.store.redis.get(module.store.key('auth'))
    assert state['sends'] == []


@pytest.mark.asyncio
@pytest.mark.parametrize('invalid', ['wrong_profile', 'text', 'timestamp', 'post_username', 'duplicate'])
async def test_unverified_history_cannot_rebind(setup, invalid):
    module, state, records = setup
    module.rebind_from_user_id = '111'
    if invalid == 'wrong_profile':
        state['wrong_profile'] = True
    elif invalid == 'duplicate':
        state['history'][1] = dict(state['history'][0])
    else:
        key = 'username' if invalid == 'post_username' else invalid
        state['history'][1][key] = 'different'
    with pytest.raises(ThreadsError):
        await module.verified_access()
    assert (await module.store.get('auth'))['user_id'] == '111'
    assert [await module.store.get(r['id']) for r in records] == records


@pytest.mark.asyncio
@pytest.mark.parametrize('blocker', ['publishing', 'reply', 'attempted'])
async def test_unsafe_queue_state_aborts_entire_migration(setup, blocker):
    module, _, records = setup
    module.rebind_from_user_id = '111'
    original = records[2]
    patch = {'status': 'publishing'} if blocker == 'publishing' else (
        {'reply_target': {'id': '999'}} if blocker == 'reply' else {'attempted_at': time.time()})
    changed = await module.store.put({**original, **patch}, original['revision'])
    with pytest.raises(ValueError):
        await module.verified_access()
    assert (await module.store.get('auth'))['user_id'] == '111'
    assert await module.store.get(original['id']) == changed


@pytest.mark.asyncio
async def test_history_changed_during_verification_aborts_migration(setup):
    module, state, records = setup
    module.rebind_from_user_id = '111'
    async def edit():
        await module.store.put({**records[0], 'text': 'changed'}, records[0]['revision'])
    state['on_read'] = edit
    with pytest.raises(RevisionConflict):
        await module.verified_access()
    assert (await module.store.get('auth'))['user_id'] == '111'
    assert await module.store.get(records[2]['id']) == records[2]


@pytest.mark.asyncio
async def test_concurrent_queue_edit_rolls_back_whole_transaction(setup, monkeypatch):
    module, _, records = setup
    module.rebind_from_user_id = '111'
    real_pipeline = module.store.redis.pipeline
    injected = False
    def racing_pipeline(*args, **kwargs):
        pipe = real_pipeline(*args, **kwargs)
        execute = pipe.execute
        async def race(*args, **kwargs):
            nonlocal injected
            if not injected:
                injected = True
                await module.store.put({**records[2], 'text': 'Concurrent edit'}, records[2]['revision'])
            return await execute(*args, **kwargs)
        pipe.execute = race
        return pipe
    monkeypatch.setattr(module.store.redis, 'pipeline', racing_pipeline)
    with pytest.raises(RevisionConflict):
        await module.verified_access()
    assert (await module.store.get('auth'))['user_id'] == '111'
    draft = await module.store.get(records[2]['id'])
    assert draft['text'] == 'Concurrent edit' and draft['user_id'] == '111'


@pytest.mark.asyncio
async def test_old_opt_in_cannot_authorize_subsequent_id_change(setup):
    module, _, _ = setup
    module.rebind_from_user_id = '111'
    await module.verified_access()
    module.seed_token = 'third-test-token'
    with pytest.raises(ThreadsError):
        await module.verified_access()
    assert (await module.store.get('auth'))['user_id'] == '222'


@pytest.mark.asyncio
async def test_pending_post_sends_once_with_new_binding_and_no_ambiguous_retry(setup, monkeypatch):
    module, state, records = setup
    module.rebind_from_user_id = '111'
    monkeypatch.setattr(time, 'time', lambda: records[2]['publish_at_ts'] + 1)
    await module.run_due()
    await module.run_due()
    assert state['sends'] == [('create', '222'), ('publish', '222')]
    assert (await module.store.get(records[2]['id']))['status'] == 'published'
    assert await module.store.get(records[3]['id']) == records[3]
