import base64
import hashlib
import json
from urllib.parse import parse_qs, urlsplit
from unittest.mock import AsyncMock
from types import SimpleNamespace

import fakeredis
import httpx
import pytest
from mcp.shared.auth import OAuthClientInformationFull

from oauth_storage import build_oauth_storage
from oauth_recovery import recovery_clients_from_env
from server import build


@pytest.fixture
def shared_redis(monkeypatch):
    shared = fakeredis.FakeServer()
    clients = []

    def connect(*args, **kwargs):
        client = fakeredis.aioredis.FakeRedis(server=shared, decode_responses=True)
        clients.append(client)
        return client

    monkeypatch.setattr('oauth_storage.Redis.from_url', connect)
    monkeypatch.setenv('OAUTH_REDIS_URL', 'redis://test-redis:6379')
    monkeypatch.setenv('GITHUB_CLIENT_ID', 'test-id')
    monkeypatch.setenv('GITHUB_CLIENT_SECRET', 'test-secret')
    monkeypatch.setenv('FASTMCP_SERVER_AUTH_OAUTH_PROXY_JWT_SIGNING_KEY',
                       'test-signing-key-for-local-tests-only')
    monkeypatch.setenv('BASE_URL', 'https://test.example')
    monkeypatch.delenv('OAUTH_RECOVERY_CLIENTS', raising=False)
    return shared, clients


@pytest.mark.asyncio
async def test_registration_and_encrypted_tokens_survive_new_server(shared_redis):
    _, clients = shared_redis
    first = build()
    await first.auth.register_client(OAuthClientInformationFull(
        client_id='test-restart-client',
        redirect_uris=['https://client.example/callback'],
        grant_types=['authorization_code', 'refresh_token'],
        response_types=['code'], scope='read:user',
    ))
    await first.auth._client_storage.put(
        key='test-token', collection='mcp-upstream-tokens',
        value={'access_token': 'upstream-secret-test-only'}, ttl=3600,
    )

    # A new server has no local state; only the external store and key are shared.
    second = build()
    registered = await second.auth.get_client('test-restart-client')
    assert registered.client_id == 'test-restart-client'
    assert str(registered.redirect_uris[0]) == 'https://client.example/callback'
    assert await second.auth.get_client('unregistered-client') is None
    assert await second.auth._client_storage.get(
        key='test-token', collection='mcp-upstream-tokens'
    ) == {'access_token': 'upstream-secret-test-only'}

    for key in await clients[0].keys('*'):
        stored = await clients[0].get(key)
        assert 'upstream-secret-test-only' not in stored
        assert 'https://client.example/callback' not in stored
    for client in clients:
        await client.aclose()


RECOVERY_ID = '11111111-1111-4111-8111-111111111111'
RECOVERY_URI = 'https://chatgpt.com/connector/oauth/test-callback'


def configure_recovery(monkeypatch):
    monkeypatch.setenv('OAUTH_RECOVERY_CLIENTS', json.dumps([
        {'client_id': RECOVERY_ID, 'redirect_uri': RECOVERY_URI}
    ]))


@pytest.mark.parametrize('value', [
    'not-json', '{}', '[null]', '[{}]',
    json.dumps([{'client_id': 'unknown', 'redirect_uri': RECOVERY_URI}]),
    *[json.dumps([{'client_id': RECOVERY_ID, 'redirect_uri': uri}]) for uri in [
        'https://evil.example/callback',
        'https://chatgpt.com.evil.example/connector/oauth/test',
        'http://chatgpt.com/connector/oauth/test',
        'https://chatgpt.com/connector/oauth/*',
        'https://chatgpt.com/connector/oauth/test?redirect=elsewhere',
    ]],
    json.dumps([{'client_id': RECOVERY_ID, 'redirect_uri': RECOVERY_URI,
                 'client_secret': 'do-not-restore-secrets'}]),
])
def test_recovery_configuration_is_explicit_and_exact(monkeypatch, value):
    monkeypatch.setenv('OAUTH_RECOVERY_CLIENTS', value)
    with pytest.raises(ValueError, match='Invalid OAUTH_RECOVERY_CLIENTS'):
        recovery_clients_from_env()


@pytest.mark.asyncio
async def test_recovered_registration_persists_without_recovery_config(shared_redis, monkeypatch):
    _, clients = shared_redis
    configure_recovery(monkeypatch)
    first = build()
    assert await first.auth.get_client('not-configured') is None
    restored = await first.auth.get_client(RECOVERY_ID)
    assert restored.client_secret is None
    assert restored.allowed_redirect_uri_patterns == []

    # The recovery setting is not needed after restoration, including after a restart.
    monkeypatch.delenv('OAUTH_RECOVERY_CLIENTS')
    second = build()
    persisted = await second.auth.get_client(RECOVERY_ID)
    assert persisted.model_dump() == restored.model_dump()
    assert await second.auth.get_client('not-configured') is None
    for key in await clients[0].keys('*'):
        assert RECOVERY_URI not in await clients[0].get(key)
    for client in clients:
        await client.aclose()


@pytest.mark.asyncio
async def test_recovery_refuses_to_replace_conflicting_registration(shared_redis, monkeypatch):
    _, clients = shared_redis
    auth = build().auth
    await auth.register_client(OAuthClientInformationFull(
        client_id=RECOVERY_ID, redirect_uris=['https://other.example/callback'],
        grant_types=['authorization_code', 'refresh_token'], scope='read:user',
    ))
    configure_recovery(monkeypatch)
    with pytest.raises(RuntimeError, match='conflicts'):
        await build().auth.get_client(RECOVERY_ID)
    assert str((await auth.get_client(RECOVERY_ID)).redirect_uris[0]) == 'https://other.example/callback'
    for client in clients:
        await client.aclose()


@pytest.mark.asyncio
async def test_recovery_authorization_still_requires_exact_callback_pkce_and_consent(shared_redis, monkeypatch):
    _, clients = shared_redis
    configure_recovery(monkeypatch)
    mcp = build()
    app = mcp.http_app(stateless_http=True)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(b'test-verifier-only').digest()).decode().rstrip('=')
    query = dict(client_id=RECOVERY_ID, redirect_uri=RECOVERY_URI,
                 response_type='code', scope='read:user', code_challenge=challenge,
                 code_challenge_method='S256', state='test-state')
    async with app.lifespan(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                    base_url='https://test.example') as client:
            unknown = await client.get('/authorize', params={**query, 'client_id': 'unknown'})
            assert unknown.status_code == 400
            for uri in ['https://evil.example/callback', RECOVERY_URI + '-other']:
                rejected = await client.get('/authorize', params={**query, 'redirect_uri': uri})
                assert rejected.status_code == 400
                assert 'location' not in rejected.headers
            no_pkce = {k: v for k, v in query.items() if not k.startswith('code_challenge')}
            rejected = await client.get('/authorize', params=no_pkce)
            assert rejected.status_code == 302
            assert parse_qs(urlsplit(rejected.headers['location']).query)['error'] == ['invalid_request']
            scope = await client.get('/authorize', params={**query, 'scope': 'repo'})
            assert 'invalid_scope' in scope.headers.get('location', scope.text)

            response = await client.get('/authorize', params=query)
            assert response.status_code == 302
            consent_url = response.headers['location']
            assert urlsplit(consent_url).path == '/consent'
            consent = await client.get(consent_url)
            assert consent.status_code == 200
            assert 'Client Not Registered' not in consent.text
            txn_id = parse_qs(urlsplit(consent_url).query)['txn_id'][0]
            assert (await client.post('/consent', data={
                'txn_id': txn_id, 'action': 'approve', 'csrf_token': 'wrong',
            })).status_code == 400
            assert (await client.get('/auth/callback', params={
                'state': 'unknown', 'code': 'not-real',
            })).status_code == 400
            assert (await client.post('/mcp', json={'method': 'tools/list'})).status_code == 401
    for client in clients:
        await client.aclose()


@pytest.mark.asyncio
async def test_recovered_client_finishes_oauth_across_server_restart(shared_redis, monkeypatch):
    _, clients = shared_redis
    configure_recovery(monkeypatch)
    verifier = 'local-test-pkce-verifier-with-more-than-43-characters'
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
    first = build()
    app = first.http_app(stateless_http=True)
    async with app.lifespan(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                    base_url='https://test.example') as client:
            response = await client.get('/authorize', params={
                'client_id': RECOVERY_ID, 'redirect_uri': RECOVERY_URI,
                'response_type': 'code', 'scope': 'read:user', 'state': 'original-state',
                'code_challenge': challenge, 'code_challenge_method': 'S256',
            })
            consent_url = response.headers['location']
            assert (await client.get(consent_url)).status_code == 200
            txn_id = parse_qs(urlsplit(consent_url).query)['txn_id'][0]
            transaction = await first.auth._transaction_store.get(key=txn_id)
            approved = await client.post('/consent', data={
                'txn_id': txn_id, 'csrf_token': transaction.csrf_token, 'action': 'approve',
            })
            upstream = urlsplit(approved.headers['location'])
            assert upstream.netloc == 'github.com'
            assert parse_qs(upstream.query)['state'] == [txn_id]

    # GitHub is mocked: no actual login, approval, credentials or messages.
    monkeypatch.setattr('fastmcp.server.auth.oauth_proxy.AsyncOAuth2Client',
                        lambda **kwargs: SimpleNamespace(fetch_token=AsyncMock(return_value={
                            'access_token': 'test-upstream-only', 'token_type': 'Bearer',
                            'expires_in': 3600, 'scope': 'read:user',
                        })))
    monkeypatch.delenv('OAUTH_RECOVERY_CLIENTS')
    second = build()
    second_app = second.http_app(stateless_http=True)
    async with second_app.lifespan(second_app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=second_app),
                                    base_url='https://test.example') as client:
            callback = await client.get('/auth/callback', params={'code': 'fake-github-code', 'state': txn_id})
            assert callback.status_code == 302
            redirect = urlsplit(callback.headers['location'])
            assert redirect.scheme + '://' + redirect.netloc + redirect.path == RECOVERY_URI
            assert parse_qs(redirect.query)['state'] == ['original-state']
            code = parse_qs(redirect.query)['code'][0]
            token = await client.post('/token', data={
                'grant_type': 'authorization_code', 'client_id': RECOVERY_ID,
                'code': code, 'redirect_uri': RECOVERY_URI, 'code_verifier': verifier,
            })
            assert token.status_code == 200
            assert token.json()['access_token'] != 'test-upstream-only'
            assert (await client.get('/auth/callback', params={
                'code': 'fake-github-code', 'state': txn_id,
            })).status_code == 400
    for client in clients:
        await client.aclose()


def test_render_cannot_fall_back_to_ephemeral_storage(monkeypatch):
    monkeypatch.setenv('RENDER_EXTERNAL_URL', 'https://test.onrender.com')
    monkeypatch.delenv('OAUTH_REDIS_URL', raising=False)
    with pytest.raises(RuntimeError, match='OAUTH_REDIS_URL'):
        build_oauth_storage('test-signing-key')


@pytest.mark.asyncio
async def test_shared_storage_health_and_auth_fail_closed(shared_redis):
    shared, clients = shared_redis
    app = build().http_app(stateless_http=True)
    async with app.lifespan(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                    base_url='https://test.example') as client:
            assert (await client.get('/health')).json() == {
                'status': 'ok', 'oauth_storage': 'redis'}
            assert (await client.post('/mcp', json={'method': 'tools/list'})).status_code == 401
            shared.connected = False
            health = await client.get('/health')
            assert health.status_code == 503
            assert health.json() == {'status': 'oauth_storage_unavailable'}
            assert (await client.post('/mcp', json={'method': 'tools/list'})).status_code == 401
    for client in clients:
        await client.aclose()
