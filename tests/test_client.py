import httpx
import pytest
from jsonschema import ValidationError
from client import MessageHelp, prepare, revision, changed_fields_match


def test_limit_merge_preserves_nested_config_without_mutation():
    current = {'config': {'limit': '', 'crm': {'enabled': True}}, 'active': False}
    result = prepare(current, 'before', {'message_limit_per_hour': 3})
    assert result['config'] == {'limit': '3', 'crm': {'enabled': True}}
    assert current['config']['limit'] == ''
    assert 'message_limit_per_hour' not in result
    assert changed_fields_match(result, {'message_limit_per_hour': 3})


@pytest.mark.parametrize('value', [True, 0, -1, 11, '3', 2.5])
def test_invalid_limits_are_rejected(value):
    with pytest.raises(ValueError):
        prepare({}, 'before', {'message_limit_per_hour': value})


def test_enum_verification_accepts_vendor_integer():
    assert changed_fields_match({'notice_confirmed_records': 2}, {'notice_confirmed_records': '2'})
    assert not changed_fields_match({'notice_confirmed_records': 1}, {'notice_confirmed_records': '2'})


@pytest.mark.asyncio
async def test_reference_data_redacts_sensitive_channel_and_settings_fields():
    def handle(request):
        path=request.url.path
        if path.endswith('/config/general'):
            data={'min_hour': 9, 'max_hour': 21, 'timezone_name': 'Asia/Dushanbe', 'testphone': 'secret-phone', 'token': 'secret-token'}
        elif path.endswith('/channels/'):
            data=[{'id': 4, 'uuid': '../escape', 'name': 'WhatsApp', 'settings': {'token': 'secret-token'}, 'instance_phone': 'secret-phone'}]
        elif path.endswith('/categories'):
            data={'categories': [{'id': 1,'title': 'Service','secret': 'secret-token'}], 'services': []}
        else:
            data=[]
        return httpx.Response(200,json={'success': True, 'data': data})
    api=MessageHelp('fake',[1],transport=httpx.MockTransport(handle))
    result=await api.reference_data(1)
    assert result['general']['min_hour']==9
    assert result['general']['test_phone_configured'] is True
    assert 'secret-' not in str(result)
    assert result['channels'][0]['bot_messages_unavailable'] is True
    with pytest.raises(ValueError):
        await api.reference_data(2)


def test_preserves_existing_settings():
    current = {'id': 4, 'bot_id': 7, 'message': 'old', 'hours': 24,
               'conditionrule': [{'unknown': 'vendor data'}], 'active': True}
    result = prepare(current, 'before', {'message': 'new'})
    assert result == {'message': 'new', 'hours': 24, 'conditionrule': current['conditionrule'], 'active': True}


@pytest.mark.parametrize('changes', [{'id': 9}, {'conditionrule': []}, {'config': {}}, {'not_a_field': 3}])
def test_rejects_unverified_fields(changes):
    with pytest.raises(ValueError):
        prepare({}, 'before', changes)


def test_rejects_invalid_hours():
    with pytest.raises(ValidationError):
        prepare({}, 'before', {'hours': 0})


@pytest.mark.asyncio
async def test_readonly_mode_never_writes():
    api = MessageHelp('fake', [1])
    with pytest.raises(ValueError, match='отключена'):
        await api.update(1, 'before', 2, {'message': 'new'}, 'anything')


def test_project_allowlist_and_path():
    api = MessageHelp('fake', [1])
    with pytest.raises(ValueError):
        api.path(2, 'before', 1)
    with pytest.raises(ValueError):
        api.path(1, '../send', 1)


@pytest.mark.asyncio
async def test_stale_revision_never_writes():
    calls = []
    def handle(request):
        calls.append(request.method)
        return httpx.Response(200, json={'success': True, 'data': {'message': 'someone changed it'}})
    api = MessageHelp('fake', [1], True, httpx.MockTransport(handle))
    with pytest.raises(ValueError, match='изменился'):
        await api.update(1, 'before', 2, {'message': 'new'}, revision({'message': 'old'}))
    assert calls == ['GET']


@pytest.mark.asyncio
async def test_update_verifies_and_preserves_other_fields():
    import json
    current = {'id': 2, 'message': 'old', 'hours': 24, 'active': False}
    calls = []
    def handle(request):
        assert str(request.url).startswith('https://message.help/api/app/projects/1/')
        assert request.headers['Authorization'] == 'Bearer fake'
        assert request.headers['X-Project'] == '1'
        calls.append(request.method)
        if request.method == 'PUT':
            payload = json.loads(request.content)
            assert payload == {'message': 'new', 'hours': 24, 'active': False}
            current.update(payload)
        return httpx.Response(200, json={'success': True, 'data': dict(current)})
    api = MessageHelp('fake', [1], True, httpx.MockTransport(handle))
    result = await api.update(1, 'before', 2, {'message': 'new'}, revision(current))
    assert result['verified'] is True
    assert calls == ['GET', 'PUT', 'GET']


@pytest.mark.asyncio
async def test_http_error_does_not_leak_body():
    api = MessageHelp('fake', [1], transport=httpx.MockTransport(
        lambda r: httpx.Response(401, text='secret details')))
    with pytest.raises(RuntimeError) as exc:
        await api.get(1, 'before', 2)
    assert '401' in str(exc.value) and 'secret' not in str(exc.value)
