"""Prove continuity of @astro_orbi before an explicitly enabled ID migration."""
from datetime import datetime

from threads_client import ThreadsError


async def verify_history(store, api, previous_id: str) -> list[dict]:
    published = []
    offset = 0
    while True:
        page = await store.list(offset)
        published.extend(r for r in page['data'] if r.get('status') == 'published'
                         and r.get('user_id') == previous_id and r.get('post_id')
                         and r.get('published_at') and r.get('text'))
        if page['next_offset'] is None:
            break
        offset = page['next_offset']
    if len(published) < 2:
        raise ThreadsError('Для переноса нужны две подтверждённые публикации старой привязки.')

    matches, used_post_ids, seen_cursors = {}, set(), set()
    after = ''
    for _ in range(20):
        page = await api.posts(after)
        for post in page['data']:
            if str(post.get('username', '')).casefold() != 'astro_orbi':
                continue
            try:
                dt = datetime.fromisoformat(post['timestamp'].replace('Z', '+00:00'))
                if dt.tzinfo is None:
                    continue
                posted_at = dt.timestamp()
            except (KeyError, ValueError, TypeError):
                continue
            if not post.get('id') or post['id'] in used_post_ids:
                continue
            for old in published:
                # IDs may differ across Meta apps. Exact content plus original
                # timestamp confirms history without trusting a username alone.
                if (old['id'] not in matches and
                        str(post.get('text', '')).strip() == old['text'].strip() and
                        abs(posted_at - old['published_at']) <= 120):
                    matches[old['id']] = {'draft_id': old['id'], 'revision': old['revision'],
                        'old_post_id': old['post_id'], 'new_post_id': post['id'],
                        'permalink': post.get('permalink'), 'timestamp': post['timestamp']}
                    used_post_ids.add(post['id'])
                    break
            if len(matches) >= 2:
                return list(matches.values())
        after = page.get('after')
        if not after or after in seen_cursors:
            break
        seen_cursors.add(after)
    raise ThreadsError('Новый токен не подтвердил историю @astro_orbi; перенос не выполнен.')
