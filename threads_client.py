"""Small, account-bound Threads client. No generic URL or credential tool."""
import re

import httpx


class ThreadsError(RuntimeError):
    pass


def media_id(value: str) -> str:
    if not re.fullmatch(r'[0-9]{1,40}', value):
        raise ValueError('Нужен ID, полученный из Threads API.')
    return value


def page(data: dict) -> dict:
    # Meta pagination URLs can contain access tokens. Return cursors only.
    return {'data': data.get('data', []),
            'after': data.get('paging', {}).get('cursors', {}).get('after')}


class ThreadsAPI:
    def __init__(self, token: str, transport=None):
        if not token:
            raise ThreadsError('Добавьте THREADS_ACCESS_TOKEN в Render Environment.')
        self.token = token
        self.transport = transport

    async def request(self, method: str, path: str, **values) -> dict:
        try:
            async with httpx.AsyncClient(
                base_url='https://graph.threads.net/v1.0/', timeout=20,
                follow_redirects=False, transport=self.transport,
                headers={'Authorization': f'Bearer {self.token}'},
            ) as client:
                response = await client.request(method, path,
                    **({'params': values} if method == 'GET' else {'data': values}))
                if response.status_code >= 300:
                    # Never propagate Meta's body, request URL, or raw exception.
                    raise ThreadsError(f'Threads API: HTTP {response.status_code}; проверьте доступ и разрешения.')
                result = response.json()
                if not isinstance(result, dict) or 'error' in result:
                    raise ThreadsError('Threads API вернул ошибку.')
                return result
        except (httpx.HTTPError, ValueError):
            raise ThreadsError('Ответ Threads API не получен или имеет неверный формат.') from None

    async def identity(self, expected_id: str = '') -> dict:
        result = await self.request('GET', 'me', fields='id,username')
        actual_id = media_id(str(result.get('id', '')))
        if str(result.get('username', '')).casefold() != 'astro_orbi':
            raise ThreadsError('Токен принадлежит другому аккаунту: нужен @astro_orbi.')
        if expected_id and actual_id != expected_id:
            raise ThreadsError('ID аккаунта не совпадает с сохранённым @astro_orbi.')
        return {'id': actual_id, 'username': 'astro_orbi'}

    async def posts(self, after: str = '') -> dict:
        params = {'fields': 'id,text,timestamp,permalink,username', 'limit': 25}
        if after:
            params['after'] = after
        return page(await self.request('GET', 'me/threads', **params))

    async def own_post(self, post_id: str) -> dict:
        result = await self.request('GET', media_id(post_id), fields='id,username,text,permalink')
        if str(result.get('username', '')).casefold() != 'astro_orbi':
            raise ThreadsError('Этот пост не принадлежит @astro_orbi.')
        return result

    async def replies(self, post_id: str, after: str = '') -> dict:
        await self.own_post(post_id)
        params = {'fields': 'id,text,username,timestamp', 'limit': 25}
        if after:
            params['after'] = after
        return page(await self.request('GET', f'{media_id(post_id)}/replies', **params))

    async def reply_target(self, post_id: str, reply_id: str) -> dict:
        """Find the selected comment in a conversation started by this account."""
        root = await self.own_post(post_id)
        reply_id = media_id(reply_id)
        params = {'fields': 'id,text,username,permalink', 'limit': 50}
        cursors = set()
        while True:
            result = await self.request('GET', f'{media_id(post_id)}/conversation', **params)
            for comment in result.get('data', []):
                if str(comment.get('id')) == reply_id:
                    return {'post_id': post_id, 'post_url': root.get('permalink'),
                            'id': reply_id, 'username': comment.get('username'),
                            'text': comment.get('text'), 'url': comment.get('permalink')}
            paging = result.get('paging', {})
            after = paging.get('cursors', {}).get('after')
            if not paging.get('next') or not after or after in cursors:
                raise ThreadsError('Комментарий не найден в выбранной ветке @astro_orbi.')
            cursors.add(after)
            params['after'] = after

    async def insights(self, post_id: str) -> dict:
        await self.own_post(post_id)
        return page(await self.request('GET', f'{media_id(post_id)}/insights',
                                       metric='views,likes,replies,reposts,quotes'))

    async def create_text(self, text: str, user_id: str, reply_to_id: str | None = None) -> str:
        params = {'media_type': 'TEXT', 'text': text}
        if reply_to_id is not None:
            params['reply_to_id'] = media_id(reply_to_id)
        result = await self.request('POST', f'{media_id(user_id)}/threads',
                                    **params)
        return media_id(str(result.get('id', '')))

    async def publish(self, container_id: str, user_id: str) -> str:
        result = await self.request('POST', f'{media_id(user_id)}/threads_publish',
                                    creation_id=media_id(container_id))
        return media_id(str(result.get('id', '')))

    async def refresh(self) -> dict:
        result = await self.request('GET', 'refresh_access_token', grant_type='th_refresh_token')
        if not isinstance(result.get('access_token'), str) or not result['access_token']:
            raise ThreadsError('Не удалось обновить доступ Threads.')
        expires = result.get('expires_in')
        if not isinstance(expires, int) or isinstance(expires, bool) or expires <= 0:
            raise ThreadsError('Не удалось проверить срок доступа Threads.')
        return {'access_token': result['access_token'], 'expires_in': expires}
