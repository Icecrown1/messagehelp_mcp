import copy
import json

import httpx
import pytest

from client import MessageHelp


def fixture_api():
    template = {'id': 1, 'channel_id': 7, 'message': 'reminder'}
    reply = {'id': 3, 'channel_id': 7, 'parent_id': 2, 'code': '3',
             'function': 'cancel_record', 'message': 'old', 'description': 'Cancel',
             'alternative_codes': [], 'function_settings': {'keep': True}, 'files': {}}
    calls = []

    def handle(request):
        calls.append((request.method, request.url.path))
        path = request.url.path
        if path.endswith('/templates/before/1'):
            data = template
        elif path.endswith('/channels/'):
            data = [{'id': 7, 'uuid': '00000000-0000-4000-8000-000000000001'}]
        elif path.endswith('/bot_messages'):
            data = [{'id': 2, 'code': '#ORDER_NOTICE#'}, reply]
        elif path.endswith('/bot_messages/3'):
            if request.method == 'PUT':
                body = json.loads(request.content)
                assert body['function'] == 'cancel_record'
                assert body['parent_id'] == 2
                assert body['code'] == '3'
                assert body['function_settings'] == {'keep': True}
                reply.update(body)
            data = reply
        else:
            raise AssertionError(path)
        return httpx.Response(200, json={'success': True, 'data': data})

    api = MessageHelp('fake', [9], True, httpx.MockTransport(handle))
    return api, template, reply, calls


@pytest.mark.asyncio
async def test_only_cancel_reply_text_changes_and_shared_scope_is_previewed():
    api, template, reply, calls = fixture_api()
    original = copy.deepcopy(reply)
    patch = {'cancellation_reply_message': 'New cancellation information'}
    preview = await api.preview(9, 'before', 1, patch)
    assert preview['shared_channel_reply'] is True
    assert preview['channel_id'] == 7 and preview['reply_id'] == 3
    result = await api.update(9, 'before', 1, patch, preview['revision'])
    assert result['verified'] is True
    assert reply == dict(original, message=patch['cancellation_reply_message'])
    assert [m for m, _ in calls].count('PUT') == 1
    assert template['message'] == 'reminder'


@pytest.mark.asyncio
async def test_reply_or_template_change_invalidates_preview():
    api, template, reply, calls = fixture_api()
    patch = {'cancellation_reply_message': 'new'}
    preview = await api.preview(9, 'before', 1, patch)
    reply['description'] = 'Edited elsewhere'
    with pytest.raises(ValueError, match='изменился'):
        await api.update(9, 'before', 1, patch, preview['revision'])
    assert not any(m == 'PUT' for m, _ in calls)


@pytest.mark.asyncio
async def test_missing_cancel_function_is_not_replaced():
    api, template, reply, calls = fixture_api()
    reply['function'] = 'some_other_function'
    with pytest.raises(ValueError, match='не найден'):
        await api.preview(9, 'before', 1, {'cancellation_reply_message': 'new'})
    assert not any(m == 'PUT' for m, _ in calls)


@pytest.mark.asyncio
@pytest.mark.parametrize('kind,patch', [
    ('crud', {'cancellation_reply_message': 'new'}),
    ('before', {'cancellation_reply_message': 'new', 'active': True}),
    ('before', {'cancellation_reply_message': ''}),
])
async def test_reply_patch_cannot_mix_settings_or_use_other_resource(kind, patch):
    api, _, _, calls = fixture_api()
    with pytest.raises(ValueError):
        await api.preview(9, kind, 1, patch)
    assert not calls
