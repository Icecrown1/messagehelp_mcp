import fakeredis
import httpx
import pytest
from mcp.shared.auth import OAuthClientInformationFull

from oauth_storage import build_oauth_storage
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
