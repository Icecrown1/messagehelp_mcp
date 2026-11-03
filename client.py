"""Narrow MessageHelp client. No arbitrary URLs, sends, deletions or credentials tools."""
import asyncio
import hashlib
import json
import time
from pathlib import Path
from uuid import UUID

import httpx
from jsonschema import Draft202012Validator

SCHEMAS = json.loads(Path(__file__).with_name('template_schemas.json').read_text())
UNSPECIFIED = {'conditionrule', 'confirmationrule', 'config'}
LIMIT_FIELD = 'message_limit_per_hour'


def selected_fields(value, fields):
    return {k: value[k] for k in fields if k in value} if isinstance(value, dict) else {}


def limit_value(template):
    value = (template.get('config') or {}).get('limit')
    return int(value) if isinstance(value, (str, int)) and str(value).isdigit() else None


def field_value(template, key):
    return limit_value(template) if key == LIMIT_FIELD else template.get(key)


def changed_fields_match(actual, expected):
    # API reads this documented enum as an integer and accepts it as a string.
    return all(str(actual.get(k)) == str(v) if k == 'notice_confirmed_records'
               else field_value(actual, k) == v for k, v in expected.items())


def revision(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(',', ':')).encode()).hexdigest()


def prepare(current, kind, changes):
    if kind not in SCHEMAS or not isinstance(changes, dict) or not changes:
        raise ValueError('Укажите тип шаблона и непустой объект изменений.')
    props = SCHEMAS[kind]['properties']
    for key, value in changes.items():
        if key == LIMIT_FIELD:
            if kind != 'before' or type(value) is not int or not 1 <= value <= 10:
                raise ValueError('Лимит напоминания должен быть целым числом от 1 до 10 в час.')
            continue
        if key not in props or props[key].get('readOnly') or key in UNSPECIFIED:
            raise ValueError(f'Изменение поля {key} не поддерживается.')
        Draft202012Validator(props[key]).validate(value)
    # The vendor's before enum contradicts its own description for value 3.
    # Validate changed fields only, so existing vendor values remain intact.
    payload = {k: v for k, v in current.items()
               if not props.get(k, {}).get('readOnly')}
    payload.update({k: v for k, v in changes.items() if k != LIMIT_FIELD})
    if LIMIT_FIELD in changes:
        existing_config = current.get('config')
        if existing_config is not None and not isinstance(existing_config, dict):
            raise ValueError('Неожиданный формат config; лимит не изменён.')
        payload['config'] = dict(existing_config or {}, limit=str(changes[LIMIT_FIELD]))
    return payload


class MessageHelp:
    def __init__(self, token, project_ids, writes=False, transport=None):
        self.token = token
        self.project_ids = set(project_ids)
        self.writes = writes
        self.transport = transport
        self.lock = asyncio.Lock()
        self.last_request = 0.0

    def path(self, project_id, kind, template_id=None):
        if type(project_id) is not int or project_id not in self.project_ids:
            raise ValueError('Проект не разрешён в MESSAGEHELP_PROJECT_IDS.')
        if kind not in SCHEMAS:
            raise ValueError('Неизвестный тип шаблона.')
        path = f'app/projects/{project_id}/integrations/altegio/templates/{kind}'
        if template_id is not None:
            if type(template_id) is not int or template_id < 1:
                raise ValueError('ID шаблона должен быть положительным числом.')
            path += f'/{template_id}'
        return path

    async def request(self, method, path, project_id, **kwargs):
        if not self.token:
            raise RuntimeError('Задайте MESSAGEHELP_TOKEN в настройках Render.')
        # At most ~171 requests/minute, below both vendor limits.
        async with self.lock:
            await asyncio.sleep(max(0, .35 - (time.monotonic() - self.last_request)))
            self.last_request = time.monotonic()
            try:
                async with httpx.AsyncClient(base_url='https://message.help/api/',
                        transport=self.transport, timeout=25, follow_redirects=False,
                        headers={'Authorization': f'Bearer {self.token}',
                                 'X-Project': str(project_id)}) as client:
                    r = await client.request(method, path, **kwargs)
            except httpx.HTTPError:
                raise RuntimeError('Ошибка связи с MessageHelp. Запись автоматически не повторяется.') from None
        if r.status_code >= 300:
            raise RuntimeError(f'MessageHelp: HTTP {r.status_code}. Запрос не повторён.')
        try:
            data = r.json()
        except ValueError:
            raise RuntimeError('MessageHelp вернул ответ не в формате JSON.') from None
        if not isinstance(data, dict) or data.get('success') is not True:
            raise RuntimeError('MessageHelp не подтвердил успешное выполнение.')
        return data

    async def get(self, project_id, kind, template_id):
        result = await self.request('GET', self.path(project_id, kind, template_id), project_id)
        value = result.get('data')
        if not isinstance(value, dict):
            raise RuntimeError('Неожиданный формат шаблона MessageHelp.')
        return value

    async def reference_data(self, project_id):
        """Read-only allowlisted reference endpoints, without credentials/contacts."""
        self.path(project_id, 'before')
        prefix = f'app/projects/{project_id}'
        result = {}
        specs = [
            ('categories', '/integrations/altegio/categories'),
            ('staffs', '/integrations/altegio/staffs'),
            ('client_categories', '/integrations/altegio/client_categories'),
            ('record_categories', '/integrations/altegio/record_categories'),
            ('general', '/config/general'),
            ('channels', '/channels/'),
        ]
        for name, suffix in specs:
            try:
                response = await self.request('GET', prefix + suffix, project_id)
                data = response.get('data')
                if name == 'general':
                    result[name] = selected_fields(data, ['timezone_name', 'min_hour', 'max_hour'])
                    result[name]['test_phone_configured'] = bool(data.get('testphone')) if isinstance(data, dict) else False
                elif name == 'categories':
                    result[name] = {k: [selected_fields(x, ['id', 'title', 'category_id', 'filials']) for x in v]
                                    for k, v in (data or {}).items() if k in {'services', 'categories'} and isinstance(v, list)}
                elif name == 'channels':
                    result[name] = []
                    for ch in data if isinstance(data, list) else []:
                        item = selected_fields(ch, ['id', 'name', 'uuid', 'active', 'type'])
                        result[name].append(item)
                        try:
                            channel_uuid = str(UUID(ch['uuid']))
                            menu = await self.request('GET', prefix + f'/channels/{channel_uuid}/bot_messages', project_id)
                            menu_data = menu.get('data')
                            item['bot_messages'] = [selected_fields(x, ['id', 'parent_id', 'code', 'alternative_codes', 'description', 'message', 'function'])
                                                    for x in menu_data if isinstance(x, dict)] if isinstance(menu_data, list) else []
                        except (ValueError, KeyError, TypeError, RuntimeError):
                            item['bot_messages_unavailable'] = True
                else:
                    fields = ['id', 'name', 'title', 'specialization', 'hidden', 'filial']
                    result[name] = [selected_fields(x, fields) for x in data] if isinstance(data, list) else []
            except (RuntimeError, ValueError, TypeError, AttributeError):
                result[name] = {'unavailable': True}
        return result

    async def list(self, project_id, kind, page=1):
        if type(page) is not int or page < 1:
            raise ValueError('Номер страницы должен быть положительным.')
        return await self.request('GET', self.path(project_id, kind), project_id,
                                  params={'page': page, 'count': 50})

    async def preview(self, project_id, kind, template_id, changes):
        current = await self.get(project_id, kind, template_id)
        prepare(current, kind, changes)
        return {'revision': revision(current), 'changes': {
            k: {'before': field_value(current, k), 'after': v} for k, v in changes.items()},
            'writes_enabled': self.writes}

    async def update(self, project_id, kind, template_id, changes, expected_revision):
        if not self.writes:
            raise ValueError('Запись отключена. ENABLE_WRITES=true включает редактирование.')
        current = await self.get(project_id, kind, template_id)
        if revision(current) != expected_revision:
            raise ValueError('Шаблон изменился. Сначала получите новое превью.')
        payload = prepare(current, kind, changes)
        await self.request('PUT', self.path(project_id, kind, template_id), project_id, json=payload)
        verified = await self.get(project_id, kind, template_id)
        if not changed_fields_match(verified, changes):
            raise RuntimeError('Запись выполнена, но проверка не совпала. Не повторяйте: перечитайте шаблон.')
        return {'verified': True, 'template': verified, 'revision': revision(verified)}
