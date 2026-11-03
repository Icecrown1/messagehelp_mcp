"""Encrypted OAuth state shared across web-service deployments."""
import os

from key_value.aio.stores.redis import RedisStore
from key_value.aio.wrappers.encryption import FernetEncryptionWrapper
from redis.asyncio import Redis


def build_oauth_storage(signing_key: str):
    url = os.environ.get('OAUTH_REDIS_URL')
    if not url:
        if os.environ.get('RENDER_EXTERNAL_URL'):
            raise RuntimeError('Задайте OAUTH_REDIS_URL: локальное OAuth-хранилище Render непостоянно.')
        return None  # FastMCP's encrypted disk storage for local development.
    if not url.startswith(('redis://', 'rediss://')):
        raise ValueError('OAUTH_REDIS_URL должен использовать redis:// или rediss://.')
    # Use redis-py's URL parser so TLS, username and password are all preserved.
    client = Redis.from_url(url, decode_responses=True, socket_connect_timeout=5,
                            socket_timeout=5, max_connections=10)
    return FernetEncryptionWrapper(
        key_value=RedisStore(client=client),
        source_material=signing_key,
        salt='stella-messagehelp-oauth-storage-v1',
    )
