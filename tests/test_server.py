import httpx
import pytest

from server import build, setup_app


@pytest.mark.asyncio
async def test_setup_mode_exposes_no_mcp_or_oauth():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=setup_app()),
                                base_url='https://test.example') as client:
        response = await client.get('/health')
        assert response.status_code == 200
        assert response.json() == {'status': 'setup_required', 'mcp_enabled': False}
        assert (await client.post('/mcp', json={'method': 'tools/call'})).status_code == 503
        assert (await client.get('/.well-known/oauth-authorization-server')).status_code == 404


def test_normal_mode_requires_auth_secrets(monkeypatch):
    for key in ['GITHUB_CLIENT_ID', 'GITHUB_CLIENT_SECRET',
                'FASTMCP_SERVER_AUTH_OAUTH_PROXY_JWT_SIGNING_KEY']:
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(RuntimeError, match='GITHUB_CLIENT_ID'):
        build()


@pytest.mark.asyncio
async def test_mcp_requires_authentication(monkeypatch):
    monkeypatch.setenv('GITHUB_CLIENT_ID', 'test-id')
    monkeypatch.setenv('GITHUB_CLIENT_SECRET', 'test-secret')
    monkeypatch.setenv('FASTMCP_SERVER_AUTH_OAUTH_PROXY_JWT_SIGNING_KEY', 'test-signing-key-for-local-tests-only')
    monkeypatch.setenv('BASE_URL', 'https://test.example')
    app = build().http_app(stateless_http=True)
    async with app.lifespan(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                    base_url='https://test.example') as client:
            assert (await client.get('/health')).status_code == 200
            assert (await client.post('/mcp', json={'method': 'tools/list'})).status_code == 401
