import os
from typing import Literal

from fastmcp import FastMCP
from fastmcp.server.dependencies import get_access_token
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

from client import MessageHelp
from oauth_storage import build_oauth_storage
from oauth_recovery import RecoverableGitHubProvider, recovery_clients_from_env

Kind = Literal['before', 'after', 'review', 'crud', 'birthday']


def setup_app():
    """Explicit bootstrap mode: expose status only, without MCP or API access."""
    async def status(request):
        return JSONResponse({'status': 'setup_required', 'mcp_enabled': False})

    async def unavailable(request):
        return JSONResponse({'error': 'MCP setup is incomplete'}, status_code=503)

    return Starlette(routes=[Route('/', status), Route('/health', status),
                             Route('/mcp', unavailable, methods=['GET', 'POST', 'DELETE'])])


def build():
    required = ['GITHUB_CLIENT_ID', 'GITHUB_CLIENT_SECRET', 'FASTMCP_SERVER_AUTH_OAUTH_PROXY_JWT_SIGNING_KEY']
    missing = [k for k in required if not os.environ.get(k)]
    if missing:
        raise RuntimeError('Заполните переменные окружения: ' + ', '.join(missing))
    base_url = os.environ.get('BASE_URL') or os.environ.get('RENDER_EXTERNAL_URL')
    if not base_url:
        raise RuntimeError('Задайте BASE_URL или RENDER_EXTERNAL_URL.')
    signing_key = os.environ['FASTMCP_SERVER_AUTH_OAUTH_PROXY_JWT_SIGNING_KEY']
    storage = build_oauth_storage(signing_key)
    auth = RecoverableGitHubProvider(recovery_clients=recovery_clients_from_env(),
                          client_id=os.environ['GITHUB_CLIENT_ID'],
                          client_secret=os.environ['GITHUB_CLIENT_SECRET'], base_url=base_url,
                          required_scopes=['read:user'],
                          jwt_signing_key=signing_key, client_storage=storage)
    mcp = FastMCP('STELLA MessageHelp', auth=auth,
                 instructions='Read templates, preview exact changes, then apply only user-requested edits. '
                 'Never invent IDs. Updates require the revision returned by preview. '
                 'This server cannot send messages or delete templates.')
    api = MessageHelp(os.environ.get('MESSAGEHELP_TOKEN', ''),
                      [int(x.strip()) for x in os.environ.get('MESSAGEHELP_PROJECT_IDS', '').split(',') if x.strip()],
                      writes=os.environ.get('ENABLE_WRITES', 'false').lower() == 'true')
    allowed = os.environ.get('ALLOWED_GITHUB_LOGIN', 'Icecrown1').casefold()

    def authorize():
        token = get_access_token()
        if token is None or str(token.claims.get('login', '')).casefold() != allowed:
            raise PermissionError('Этот GitHub-аккаунт не имеет доступа к MessageHelp.')

    @mcp.custom_route('/health', methods=['GET'])
    async def health(request):
        if storage is not None:
            try:
                await storage.get(key='health-check', collection='mcp-oauth-health')
            except Exception:
                return JSONResponse({'status': 'oauth_storage_unavailable'}, status_code=503)
        return JSONResponse({'status': 'ok', 'oauth_storage': 'redis' if storage else 'disk'})

    @mcp.tool(annotations={'readOnlyHint': True})
    async def list_templates(project_id: int, kind: Kind, page: int = 1) -> dict:
        """Список шаблонов Altegio; страницы по 50 записей. Типы: before/after/review/crud/birthday."""
        authorize()
        return await api.list(project_id, kind, page)

    @mcp.tool(annotations={'readOnlyHint': True})
    async def get_template(project_id: int, kind: Kind, template_id: int) -> dict:
        """Прочитать существующий шаблон со всеми его настройками."""
        authorize()
        return {'template': await api.get(project_id, kind, template_id),
                'reference_data': await api.reference_data(project_id)}

    @mcp.tool(annotations={'readOnlyHint': True})
    async def preview_changes(project_id: int, kind: Kind, template_id: int, changes: dict) -> dict:
        """Показать точные изменения и revision; ничего не сохраняет.

        message_limit_per_hour (1..10) меняет только config.limit напоминания before.
        Остальные вложенные настройки не меняются.
        """
        authorize()
        return await api.preview(project_id, kind, template_id, changes)

    @mcp.tool(annotations={'readOnlyHint': False, 'destructiveHint': True, 'idempotentHint': False})
    async def update_template(project_id: int, kind: Kind, template_id: int,
                              changes: dict, expected_revision: str) -> dict:
        """Сохранить согласованные изменения существующего шаблона и проверить результат.

        Изменение активного шаблона влияет на будущие автоматические сообщения MessageHelp.
        Требует ENABLE_WRITES=true и revision из последнего preview_changes.
        """
        authorize()
        return await api.update(project_id, kind, template_id, changes, expected_revision)

    return mcp


if __name__ == '__main__':
    port = int(os.environ.get('PORT', '10000'))
    if os.environ.get('SETUP_MODE', 'false').lower() == 'true':
        import uvicorn
        uvicorn.run(setup_app(), host='0.0.0.0', port=port)
    else:
        build().run(transport='http', host='0.0.0.0', port=port, stateless_http=True)
