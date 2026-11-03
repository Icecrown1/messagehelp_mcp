"""Restore explicitly configured public ChatGPT clients after lost DCR storage.

This only restores registration metadata. GitHub login, consent, PKCE and the
application's account allowlist still apply; no tokens or sessions are restored.
"""
import asyncio
import json
import os
import re
from uuid import UUID

from fastmcp.server.auth.oauth_proxy import ProxyDCRClient
from fastmcp.server.auth.providers.github import GitHubProvider


def recovery_clients_from_env() -> dict[str, str]:
    try:
        entries = json.loads(os.environ.get('OAUTH_RECOVERY_CLIENTS', '[]'))
        if not isinstance(entries, list) or len(entries) > 10:
            raise ValueError
        result = {}
        for entry in entries:
            if not isinstance(entry, dict) or set(entry) != {'client_id', 'redirect_uri'}:
                raise ValueError
            client_id, redirect_uri = entry['client_id'], entry['redirect_uri']
            if not isinstance(client_id, str) or str(UUID(client_id)) != client_id:
                raise ValueError
            if not isinstance(redirect_uri, str) or not re.fullmatch(
                r'https://chatgpt\.com/connector/oauth/[A-Za-z0-9_-]+', redirect_uri
            ):
                raise ValueError
            if client_id in result:
                raise ValueError
            result[client_id] = redirect_uri
        return result
    except (ValueError, TypeError, AttributeError):
        raise ValueError('Invalid OAUTH_RECOVERY_CLIENTS configuration') from None


class RecoverableGitHubProvider(GitHubProvider):
    def __init__(self, *, recovery_clients: dict[str, str], **kwargs):
        super().__init__(**kwargs)
        self._recovery_clients = dict(recovery_clients)
        self._recovery_lock = asyncio.Lock()

    async def get_client(self, client_id):
        if client_id not in self._recovery_clients:
            return await super().get_client(client_id)

        # This service runs one instance. Recheck under the lock to avoid two
        # simultaneous reconnects restoring the same missing registration.
        async with self._recovery_lock:
            client = await super().get_client(client_id)
            redirect_uri = self._recovery_clients[client_id]
            if client is None:
                client = ProxyDCRClient(
                    client_id=client_id,
                    client_secret=None,
                    client_name='STELLA MessageHelp (ChatGPT)',
                    redirect_uris=[redirect_uri],
                    grant_types=['authorization_code', 'refresh_token'],
                    response_types=['code'],
                    scope='read:user',
                    token_endpoint_auth_method='none',
                    allowed_redirect_uri_patterns=[],
                )
                await self._client_store.put(key=client_id, value=client)
            elif (
                client.client_secret is not None
                or client.token_endpoint_auth_method != 'none'
                or client.scope != 'read:user'
                or [str(uri) for uri in client.redirect_uris or []] != [redirect_uri]
                or set(client.grant_types or []) != {'authorization_code', 'refresh_token'}
            ):
                # Never replace a conflicting registration or discard a secret.
                raise RuntimeError('OAuth recovery metadata conflicts with stored client')

            # Recovered clients accept only their exact operator-pinned callback,
            # even if the general DCR provider allows broader redirect patterns.
            client.allowed_redirect_uri_patterns = []
            return client
