"""Encrypted Astro Orbi records with Redis WATCH transactions and durable queues."""
from __future__ import annotations

import base64
import json
import re
import time
from uuid import uuid4

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from redis.exceptions import WatchError


class RevisionConflict(ValueError):
    pass


class ThreadsStore:
    prefix = 'astro-orbi:threads:v1:'

    def __init__(self, redis, signing_key: str):
        self.redis = redis
        key = HKDF(algorithm=hashes.SHA256(), length=32,
                   salt=b'astro-orbi-threads-store-v1', info=b'records').derive(signing_key.encode())
        self.cipher = Fernet(base64.urlsafe_b64encode(key))

    def key(self, record_id: str) -> str:
        if record_id != 'auth' and not re.fullmatch(r'[a-f0-9]{32}', record_id):
            raise ValueError('Неверный ID черновика.')
        return self.prefix + record_id

    def decode(self, raw) -> dict | None:
        if raw is None:
            return None
        return json.loads(self.cipher.decrypt(raw.encode() if isinstance(raw, str) else raw))

    async def get(self, record_id: str) -> dict:
        result = self.decode(await self.redis.get(self.key(record_id)))
        if result is None:
            raise ValueError('Запись не найдена.')
        return result

    async def put(self, record: dict, expected_revision: str | None) -> dict:
        record = {**record, 'revision': uuid4().hex, 'updated_at': time.time()}
        key = self.key(record['id'])
        try:
            async with self.redis.pipeline(transaction=True) as pipe:
                await pipe.watch(key)
                current = self.decode(await pipe.get(key))
                if (current or {}).get('revision') != expected_revision:
                    raise RevisionConflict('Запись изменилась. Заново откройте предпросмотр.')
                pipe.multi()
                pipe.set(key, self.cipher.encrypt(json.dumps(record, ensure_ascii=False).encode()))
                if record['id'] != 'auth':
                    pipe.zadd(self.prefix + 'index', {record['id']: record['created_at']})
                    for index, state, score in [('due', 'queued', record.get('publish_at_ts', 0)),
                                                ('inflight', 'publishing', record['updated_at'])]:
                        if record['status'] == state:
                            pipe.zadd(self.prefix + index, {record['id']: score})
                        else:
                            pipe.zrem(self.prefix + index, record['id'])
                await pipe.execute()
        except WatchError:
            raise RevisionConflict('Запись изменилась. Заново откройте предпросмотр.') from None
        return record

    async def list(self, offset: int = 0) -> dict:
        if not 0 <= offset <= 100000:
            raise ValueError('Неверный offset.')
        ids = await self.redis.zrevrange(self.prefix + 'index', offset, offset + 24)
        records = [await self.get(i) for i in ids]
        return {'data': records, 'next_offset': offset + 25 if len(ids) == 25 else None}

    async def due(self, now: float) -> list[str]:
        return await self.redis.zrangebyscore(self.prefix + 'due', '-inf', now, start=0, num=25)

    async def abandoned(self, now: float) -> list[str]:
        return await self.redis.zrangebyscore(self.prefix + 'inflight', '-inf', now - 600,
                                             start=0, num=25)
